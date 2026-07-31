from __future__ import annotations

import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from analyze_adc_raw import (  # noqa: E402
    ADC_BITS,
    AnalysisError,
    RawCapture,
    SUPPORTED_SAMPLE_PHASES,
    analyze_capture,
    load_and_validate_capture_manifest,
    read_ila_csv,
)


SAMPLE_COUNT = 16_384
SAMPLE_RATE_HZ = 65_000_000.0
FREQUENCY_HZ = 100_000.0
TRIANGLE_FREQUENCY_HZ = 20_000.0


def physical_words(logical_words: np.ndarray, mapping: tuple[int, ...]) -> np.ndarray:
    result = np.zeros_like(logical_words, dtype=np.uint16)
    for physical_bit, logical_bit in enumerate(mapping):
        result |= (
            ((logical_words >> logical_bit) & 1).astype(np.uint16) << physical_bit
        )
    return result


def make_capture(mapping: tuple[int, ...], *, corrupt_fraction: float = 0.0) -> RawCapture:
    samples = np.arange(SAMPLE_COUNT, dtype=np.float64)
    logical_words = np.rint(
        2048.0
        + 1800.0
        * np.sin(2.0 * np.pi * FREQUENCY_HZ * samples / SAMPLE_RATE_HZ)
    ).astype(np.uint16)
    words = physical_words(logical_words, mapping)
    if corrupt_fraction:
        rng = np.random.default_rng(9226)
        count = int(round(SAMPLE_COUNT * corrupt_fraction))
        indices = rng.choice(SAMPLE_COUNT, count, replace=False)
        words[indices] = rng.integers(0, 4096, size=count, dtype=np.uint16)
    return RawCapture(
        indices=np.arange(SAMPLE_COUNT, dtype=np.int64),
        words=words,
        otr=np.zeros(SAMPLE_COUNT, dtype=np.uint8),
        data_column="probe0",
        otr_column="probe1",
    )


def make_triangle_capture(
    mapping: tuple[int, ...], *, frequency_hz: float = TRIANGLE_FREQUENCY_HZ
) -> RawCapture:
    samples = np.arange(SAMPLE_COUNT, dtype=np.float64)
    angles = (
        2.0 * np.pi * frequency_hz * samples / SAMPLE_RATE_HZ + 0.73
    )
    triangle = (2.0 / np.pi) * np.arcsin(np.sin(angles))
    logical_words = np.rint(2048.0 + 1800.0 * triangle).astype(np.uint16)
    return RawCapture(
        indices=np.arange(SAMPLE_COUNT, dtype=np.int64),
        words=physical_words(logical_words, mapping),
        otr=np.zeros(SAMPLE_COUNT, dtype=np.uint8),
        data_column="probe0",
        otr_column="probe1",
    )


class MappingAnalysisTests(unittest.TestCase):
    def analyze(self, capture: RawCapture) -> dict:
        return analyze_capture(
            capture,
            stimulus="sine",
            frequency_hz=FREQUENCY_HZ,
            sample_rate_hz=SAMPLE_RATE_HZ,
        )

    def test_direct_mapping_passes(self) -> None:
        identity = tuple(range(ADC_BITS))
        result = self.analyze(make_capture(identity))
        self.assertTrue(result["freeze_allowed"])
        self.assertEqual(result["classification"], "direct")
        self.assertEqual(
            result["winner"]["mapping_physical_to_logical"], list(identity)
        )

    def test_single_local_swap_is_identified(self) -> None:
        mapping = list(range(ADC_BITS))
        mapping[1], mapping[2] = mapping[2], mapping[1]
        result = self.analyze(make_capture(tuple(mapping)))
        self.assertTrue(result["freeze_allowed"])
        self.assertEqual(result["classification"], "local_permutation")
        self.assertEqual(result["winner"]["mapping_physical_to_logical"], mapping)

    def test_two_local_swaps_are_identified(self) -> None:
        mapping = list(range(ADC_BITS))
        mapping[1], mapping[3] = mapping[3], mapping[1]
        mapping[6], mapping[8] = mapping[8], mapping[6]
        result = self.analyze(make_capture(tuple(mapping)))
        self.assertTrue(result["freeze_allowed"])
        self.assertEqual(result["winner"]["mapping_physical_to_logical"], mapping)

    def test_three_nonadjacent_swaps_are_identified(self) -> None:
        mapping = list(range(ADC_BITS))
        for first, second in ((0, 7), (2, 9), (4, 11)):
            mapping[first], mapping[second] = mapping[second], mapping[first]
        result = self.analyze(make_capture(tuple(mapping)))
        self.assertTrue(result["freeze_allowed"])
        self.assertEqual(result["classification"], "local_permutation")
        self.assertEqual(result["winner"]["mapping_physical_to_logical"], mapping)
        self.assertEqual(
            result["permutation_search"]["method"],
            "exact_monotonic_subset_dp",
        )

    def test_nonlocal_permutation_is_identified(self) -> None:
        mapping = (3, 8, 1, 11, 0, 6, 10, 2, 9, 5, 7, 4)
        result = self.analyze(make_capture(mapping))
        self.assertTrue(result["freeze_allowed"])
        self.assertEqual(result["classification"], "local_permutation")
        self.assertEqual(
            result["winner"]["mapping_physical_to_logical"], list(mapping)
        )

    def test_nonlocal_permutation_is_identified_for_triangle(self) -> None:
        mapping = (3, 8, 1, 11, 0, 6, 10, 2, 9, 5, 7, 4)
        result = analyze_capture(
            make_triangle_capture(mapping),
            stimulus="triangle",
            frequency_hz=TRIANGLE_FREQUENCY_HZ,
            sample_rate_hz=SAMPLE_RATE_HZ,
        )
        self.assertTrue(result["freeze_allowed"])
        self.assertEqual(result["classification"], "local_permutation")
        self.assertEqual(
            result["winner"]["mapping_physical_to_logical"], list(mapping)
        )

    def test_fast_triangle_cannot_freeze_an_arbitrary_mapping(self) -> None:
        mapping = (3, 8, 1, 11, 0, 6, 10, 2, 9, 5, 7, 4)
        result = analyze_capture(
            make_triangle_capture(mapping, frequency_hz=FREQUENCY_HZ),
            stimulus="triangle",
            frequency_hz=FREQUENCY_HZ,
            sample_rate_hz=SAMPLE_RATE_HZ,
        )
        self.assertFalse(result["freeze_allowed"])
        self.assertFalse(result["gates"]["triangle_code_step_at_most_2_5"])

    def test_full_reverse_is_identified(self) -> None:
        mapping = tuple(reversed(range(ADC_BITS)))
        result = self.analyze(make_capture(mapping))
        self.assertTrue(result["freeze_allowed"])
        self.assertEqual(result["classification"], "full_reverse")
        self.assertEqual(
            result["winner"]["mapping_physical_to_logical"], list(mapping)
        )

    def test_nonstatic_mixed_words_cannot_freeze(self) -> None:
        identity = tuple(range(ADC_BITS))
        result = self.analyze(make_capture(identity, corrupt_fraction=0.02))
        self.assertFalse(result["freeze_allowed"])
        self.assertIn(result["classification"], {"unstable_nonstatic_mapping", "insufficient_evidence"})

    def test_single_mixed_word_cannot_hide_in_the_outlier_rate(self) -> None:
        identity = tuple(range(ADC_BITS))
        capture = make_capture(identity)
        capture.words[235] ^= np.uint16(1 << 9)

        result = self.analyze(capture)

        self.assertFalse(result["freeze_allowed"])
        self.assertEqual(result["classification"], "unstable_nonstatic_mapping")
        self.assertFalse(result["gates"]["catastrophic_outlier_count_zero"])
        self.assertFalse(result["gates"]["residual_step_at_most_16_codes"])
        self.assertFalse(
            result["gates"]["cycle_repeatability_max_at_most_8_codes"]
        )
        self.assertEqual(result["winner"]["outlier_count"], 1)
        self.assertGreater(result["winner"]["residual_max_abs_codes"], 500.0)


class CsvParserTests(unittest.TestCase):
    def test_reads_vivado_style_hex_vector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["Radix", "UNSIGNED", "HEX", "BIN"])
                writer.writerow(
                    [
                        "Sample in Buffer",
                        "Sample in Window",
                        "top/u_raw_iob_ila/probe0[11:0]",
                        "top/u_raw_iob_ila/probe1[0]",
                    ]
                )
                for index in range(1024):
                    writer.writerow([index, index - 512, f"{(0x800 + index) & 0xFFF:03X}", "0"])
            capture = read_ila_csv(path)
            self.assertEqual(capture.words.size, 1024)
            self.assertEqual(int(capture.words[0]), 0x800)
            self.assertEqual(int(capture.words[-1]), 0xBFF)
            self.assertEqual(int(np.count_nonzero(capture.otr)), 0)

    def test_reads_vivado_2025_split_raw_iob_slices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(
                    [
                        "Sample in Buffer",
                        "Sample in Window",
                        "top/u_raw_iob_ila/adc_data_a_iob[10:0]",
                        "top/u_raw_iob_ila/adc_data_a_iob_reg[11]_0[0:0]",
                        "top/u_raw_iob_ila/probe1[0]",
                    ]
                )
                writer.writerow(
                    ["Radix - UNSIGNED", "UNSIGNED", "HEX", "HEX", "BIN"]
                )
                for index in range(1024):
                    word = (0x600 + 3 * index) & 0xFFF
                    writer.writerow(
                        [
                            index,
                            index - 512,
                            f"{word & 0x7FF:03X}",
                            f"{(word >> 11) & 1:X}",
                            "0",
                        ]
                    )
            capture = read_ila_csv(path)
            expected = np.asarray(
                [(0x600 + 3 * index) & 0xFFF for index in range(1024)],
                dtype=np.uint16,
            )
            np.testing.assert_array_equal(capture.words, expected)
            self.assertIn("adc_data_a_iob[10:0]", capture.data_column)
            self.assertIn("adc_data_a_iob_reg[11]_0[0:0]", capture.data_column)

    def test_reads_independent_probe0_bit_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(
                    ["sample_index"]
                    + [f"top/u_raw_iob_ila/probe0[{bit}]" for bit in range(12)]
                    + ["top/u_raw_iob_ila/probe1[0]"]
                )
                for index in range(1024):
                    word = (0x800 + index) & 0xFFF
                    writer.writerow(
                        [index]
                        + [(word >> bit) & 1 for bit in range(12)]
                        + [0]
                    )
            capture = read_ila_csv(path)
            self.assertEqual(int(capture.words[0]), 0x800)
            self.assertEqual(int(capture.words[-1]), 0xBFF)

    def test_rejects_missing_data_slice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(
                    [
                        "sample_index",
                        "adc_data_a_iob[10:0]",
                        "adc_otr_a_iob",
                    ]
                )
                writer.writerow([0, "000", 0])
            with self.assertRaisesRegex(AnalysisError, "missing bits.*11"):
                read_ila_csv(path)

    def test_rejects_overlapping_data_slices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(
                    [
                        "sample_index",
                        "adc_data_a_iob[11:0]",
                        "adc_data_a_iob_reg[11]_0[0:0]",
                        "adc_otr_a_iob",
                    ]
                )
                writer.writerow([0, "2048", 1, 0])
            with self.assertRaisesRegex(AnalysisError, "bit 11 overlaps"):
                read_ila_csv(path)

    def test_rejects_duplicate_data_slice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(
                    [
                        "sample_index",
                        "adc_data_a_iob[10:0]",
                        "adc_data_a_iob_reg[11]_0[0:0]",
                        "adc_data_a_iob_reg[11]_0[0:0]",
                        "adc_otr_a_iob",
                    ]
                )
                writer.writerow([0, "0", 0, 0, 0])
            with self.assertRaisesRegex(AnalysisError, "bit 11 overlaps"):
                read_ila_csv(path)

    def test_rejects_unknown_logic_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["sample_index", "adc_data_a_iob[11:0]", "adc_otr_a_iob"])
                for index in range(1024):
                    writer.writerow([index, "XXX" if index == 10 else "2048", 0])
            with self.assertRaises(AnalysisError):
                read_ila_csv(path)


class CaptureManifestTests(unittest.TestCase):
    def write_manifest(self, directory: Path, phase: object) -> tuple[Path, Path]:
        csv_path = directory / "adc_raw_iob.csv"
        csv_path.write_bytes(b"bound capture\n")
        manifest_path = directory / "capture_manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "format": "CycleScope raw IOB ILA capture manifest v1",
                    "sample_rate_hz": 65_000_000,
                    "sample_phase_deg": phase,
                    "capture_depth": 16_384,
                    "csv": {
                        "file": csv_path.name,
                        "sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
                    },
                }
            ),
            encoding="utf-8",
        )
        return manifest_path, csv_path

    def test_accepts_supported_diagnostic_phases(self) -> None:
        for phase in SUPPORTED_SAMPLE_PHASES:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                manifest_path, csv_path = self.write_manifest(Path(directory), phase)
                manifest = load_and_validate_capture_manifest(manifest_path, csv_path)
                self.assertEqual(manifest["sample_phase_deg"], phase)

    def test_rejects_unsupported_diagnostic_phase(self) -> None:
        for phase in (1, 15, 45, 357, 360, 3000, 3540, 348.0, "348", True):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                manifest_path, csv_path = self.write_manifest(Path(directory), phase)
                with self.assertRaisesRegex(
                    AnalysisError, "sample phase is unsupported"
                ):
                    load_and_validate_capture_manifest(manifest_path, csv_path)


if __name__ == "__main__":
    unittest.main()
