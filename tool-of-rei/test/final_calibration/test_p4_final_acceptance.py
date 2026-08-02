from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


THIS_DIR = Path(__file__).resolve().parent
TEST_DIR = THIS_DIR.parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

import m12_p4_acceptance as legacy
import p4_final_acceptance as final


PROJECT_ROOT = THIS_DIR.parents[2]
FROZEN_ROOT = (
    PROJECT_ROOT
    / "tool-of-rei"
    / "evidence"
    / "final-calibration-20260801_145546+0800"
)
HISTORICAL_CASE = (
    PROJECT_ROOT
    / "tool-of-rei"
    / "evidence"
    / "m12-live-ua-ub-20260801_111910"
    / "UA-CREST-LOW"
)


class FinalAcceptanceTest(unittest.TestCase):
    def load_profile(self) -> final.FrozenResponseProfile:
        return final.load_frozen_profile(
            FROZEN_ROOT / "fit-v2",
            FROZEN_ROOT / "holdout-v2",
            FROZEN_ROOT / "p4-asset-v2",
        )

    def test_frozen_profile_matches_installed_header(self) -> None:
        profile = self.load_profile()
        self.assertEqual(profile.profile_id, 0xC5DCDE41)
        self.assertEqual(profile.response_sha256, final.EXPECTED_RESPONSE_SHA256)
        self.assertEqual(len(profile.frequencies_hz), 12)
        self.assertEqual(profile.frequencies_hz[0], 10_000.0)
        self.assertEqual(profile.frequencies_hz[-1], 500_000.0)

    def test_uart_parser_rejects_legacy_raw_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "uart.log"
            path.write_text(
                "I cyclescope_pipe: measurement: session=12345678 config=00000001 "
                "epoch=2 frame=100 gen=1 F0=40000.00Hz Vpp=100.000mV "
                "RMS=30.000mV peaks=2 P1=40000.00Hz/20.000mVpk "
                "P2=120000.00Hz/10.000mVpk P3=0.00Hz/0.000mVpk "
                "cal=1 test=0\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(final.FinalAcceptanceError, "p4cal=1"):
                final.parse_uart(path, 0xC5DCDE41)

    def test_historical_live_frames_pass_after_dg_reference_transform(self) -> None:
        if not HISTORICAL_CASE.is_dir():
            self.skipTest("historical live evidence is not present")
        profile = self.load_profile()
        original = json.loads(
            (HISTORICAL_CASE / "manifest" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        transformed = deepcopy(original)
        transformed["source"]["programmed_amplitude_scale"] = 1.0
        transformed["source"]["source_fullscale_vpp"] *= 0.5
        transformed["source"]["component_vpp_sum"] *= 0.5
        for collection in ("tones", "source_tones"):
            for tone in transformed[collection]:
                tone["peak_mv"] *= 0.5
                tone["peak_v"] *= 0.5
        for section in ("theory", "source_theory"):
            transformed[section]["voltage_peak_to_peak_v"] *= 0.5
            transformed[section]["true_rms_v"] *= 0.5
            transformed[section]["full_scale_peak_v"] *= 0.5

        # The production loader is intentionally path-based.  Write only a
        # temporary transformed manifest and use that same code below.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(transformed, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            target, tones, _manifest, _out_of_band = final.load_target_and_safety(
                manifest_path, maximum_programmed_vpp=0.25
            )
            records, frames = final.load_verified_mirror(
                HISTORICAL_CASE / "mirror", profile
            )
            active = legacy.signal_run_indices(
                frames, records, target["fundamental_hz"], tones[0]["amplitude_vpk"]
            )
            indexes = legacy.representative_indices(active)
            analyses = [
                final.corrected_analysis(frames[index], records[index], profile)
                for index in indexes
            ]
            mirror = final.summarize_analysis(analyses)
            record = records[indexes[len(indexes) // 2]]
            line_text = []
            for index in range(3):
                if index < len(mirror["lines"]):
                    line = mirror["lines"][index]
                    line_text.append(
                        f"P{index + 1}={line['frequency_hz']['median']:.2f}Hz/"
                        f"{line['amplitude_mVpk']['median']:.3f}mVpk"
                    )
                else:
                    line_text.append(f"P{index + 1}=0.00Hz/0.000mVpk")
            uart_path = root / "uart.log"
            uart_path.write_text(
                "I cyclescope_pipe: measurement: "
                f"session={int(record['session_id']):08X} "
                f"config={int(record['config_id']):08X} epoch=2 "
                f"frame={int(record['frame_id'])} gen=1 "
                f"F0={mirror['fundamental_hz']['median']:.2f}Hz "
                f"Vpp={mirror['voltage_peak_to_peak_mV']['median']:.3f}mV "
                f"RMS={mirror['true_rms_mV']['median']:.3f}mV "
                f"peaks={len(mirror['lines'])} {' '.join(line_text)} "
                "up_cal=1 test=0 p4cal=1 profile=C5DCDE41\n",
                encoding="utf-8",
            )
            output_path = root / "acceptance.json"
            command = [
                sys.executable,
                str(THIS_DIR / "p4_final_acceptance.py"),
                "--manifest",
                str(manifest_path),
                "--mirror-dir",
                str(HISTORICAL_CASE / "mirror"),
                "--uart-log",
                str(uart_path),
                "--fit-dir",
                str(FROZEN_ROOT / "fit-v2"),
                "--holdout-dir",
                str(FROZEN_ROOT / "holdout-v2"),
                "--asset-dir",
                str(FROZEN_ROOT / "p4-asset-v2"),
                "--output",
                str(output_path),
            ]
            result = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            report = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
            self.assertTrue(report["pass"], msg=report.get("failures"))
            self.assertEqual(report["p4"]["p4_response_profile_id"], "C5DCDE41")


if __name__ == "__main__":
    unittest.main()
