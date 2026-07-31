#!/usr/bin/env python3
"""Fail-closed analysis for CycleScope raw-IOB ILA captures."""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np


SAMPLE_RATE_HZ = 65_000_000.0
ADC_BITS = 12
ADC_MASK = (1 << ADC_BITS) - 1
MIN_CAPTURE_SAMPLES = 1_024
MIN_BIT_STATE_COUNT = 64
MIN_R_SQUARED = 0.995
MAX_NRMSE_FRACTION = 0.02
MIN_RUNNER_UP_NRMSE_RATIO = 1.5
MAX_OUTLIER_RATE = 0.001
MAX_CATASTROPHIC_OUTLIER_COUNT = 0
MAX_RESIDUAL_STEP_CODES = 16.0
MAX_CYCLE_REPEATABILITY_CODES = 8.0
MIN_ORDER_COMPARISON_ACCURACY = 0.99
ORDER_COMPARISON_LAGS = (1, 2, 4, 8, 16, 32, 64)
MIN_REFERENCE_DELTA = 1e-3
MAX_TRIANGLE_CODE_STEP_PER_SAMPLE = 2.5
SUPPORTED_SAMPLE_PHASES = (
    0,
    30,
    60,
    90,
    120,
    150,
    180,
    210,
    240,
    270,
    300,
    330,
    345,
    348,
    351,
    354,
)


class AnalysisError(RuntimeError):
    """Raised when evidence is malformed or cannot be interpreted safely."""


@dataclass(frozen=True)
class RawCapture:
    indices: np.ndarray
    words: np.ndarray
    otr: np.ndarray
    data_column: str
    otr_column: str


@dataclass(frozen=True)
class DataSlice:
    column: int
    logical_bits: tuple[int, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_header(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).lower()


def _parse_data_slice(value: str, column: int) -> DataSlice | None:
    register_match = re.search(
        r"(?:^|/)adc_data_a_iob_reg\[([0-9]+)\](?:_[0-9]+)?(?:\[0:0\])?$",
        value,
    )
    if register_match:
        logical_bit = int(register_match.group(1))
        if not 0 <= logical_bit < ADC_BITS:
            raise AnalysisError(
                f"ADC register slice names out-of-range bit {logical_bit}"
            )
        return DataSlice(column=column, logical_bits=(logical_bit,))

    for signal_name in ("adc_data_a_iob", "probe0"):
        match = re.search(
            rf"(?:^|/){signal_name}(?:\[([0-9]+)(?::([0-9]+))?\])?$",
            value,
        )
        if not match:
            continue
        if match.group(1) is None:
            return DataSlice(column=column, logical_bits=tuple(range(ADC_BITS)))
        most_significant = int(match.group(1))
        least_significant = (
            most_significant if match.group(2) is None else int(match.group(2))
        )
        if (
            not 0 <= least_significant < ADC_BITS
            or not 0 <= most_significant < ADC_BITS
        ):
            raise AnalysisError(
                f"ADC data slice [{most_significant}:{least_significant}] is out of range"
            )
        if most_significant < least_significant:
            raise AnalysisError(
                f"ascending ADC data slice [{most_significant}:{least_significant}] "
                "is unsupported"
            )
        return DataSlice(
            column=column,
            logical_bits=tuple(range(least_significant, most_significant + 1)),
        )
    return None


def _find_header(
    rows: list[list[str]],
) -> tuple[int, int, int, list[DataSlice]]:
    for row_index, row in enumerate(rows):
        normalized = [_normalized_header(value) for value in row]
        index_candidates = [
            index
            for index, value in enumerate(normalized)
            if value == "sample_index" or "sample in buffer" in value
        ]
        otr_candidates = [
            index
            for index, value in enumerate(normalized)
            if "adc_otr_a_iob" in value
            or re.search(r"(?:^|/)probe1(?:\[0\])?$", value)
        ]
        if len(index_candidates) != 1 or len(otr_candidates) != 1:
            continue
        data_slices = [
            data_slice
            for column, value in enumerate(normalized)
            if (data_slice := _parse_data_slice(value, column)) is not None
        ]
        if not data_slices:
            continue
        bit_sources: dict[int, int] = {}
        for data_slice in data_slices:
            for logical_bit in data_slice.logical_bits:
                if logical_bit in bit_sources:
                    raise AnalysisError(
                        f"ADC data bit {logical_bit} overlaps columns "
                        f"{bit_sources[logical_bit]} and {data_slice.column}"
                    )
                bit_sources[logical_bit] = data_slice.column
        missing_bits = sorted(set(range(ADC_BITS)) - set(bit_sources))
        if missing_bits:
            raise AnalysisError(f"ADC data slices are missing bits {missing_bits}")
        return (
            row_index,
            index_candidates[0],
            otr_candidates[0],
            data_slices,
        )
    raise AnalysisError(
        "cannot find one sample index, complete non-overlapping ADC data slices, "
        "and one probe1/OTR column"
    )


def _radix_for_column(rows: list[list[str]], header_row: int, column: int) -> str | None:
    del header_row  # Vivado versions place the Radix row before or after header.
    found: set[str] = set()
    for row in rows:
        if (
            not row
            or not _normalized_header(row[0]).startswith("radix")
            or column >= len(row)
        ):
            continue
        value = row[column].strip().upper()
        if value in {"HEX", "BIN", "BINARY", "UNSIGNED", "DEC", "DECIMAL"}:
            found.add(value)
    if len(found) > 1:
        raise AnalysisError(
            f"ILA CSV has conflicting radix declarations for column {column}: "
            f"{sorted(found)}"
        )
    return next(iter(found), None)


def _parse_integer(token: str, *, radix: str | None, width: int, label: str) -> int:
    value = token.strip().replace("_", "")
    if not value or re.search(r"[xXzZ?]", value):
        raise AnalysisError(f"{label} contains an empty/unknown value: {token!r}")
    try:
        # An explicit Vivado Radix row is authoritative. In particular, a
        # hexadecimal word such as 0B2 is data, not a Python-style 0b prefix.
        if radix == "HEX":
            parsed = int(value, 16)
        elif radix in {"BIN", "BINARY"}:
            parsed = int(value, 2)
        elif radix in {"UNSIGNED", "DEC", "DECIMAL"}:
            parsed = int(value, 10)
        elif value.lower().startswith("0x"):
            parsed = int(value, 16)
        elif value.lower().startswith("0b"):
            parsed = int(value, 2)
        else:
            parsed = int(value, 10)
    except ValueError as exc:
        raise AnalysisError(f"{label} is not a valid integer: {token!r}") from exc
    if parsed < 0 or parsed >= (1 << width):
        raise AnalysisError(f"{label} is outside {width}-bit range: {parsed}")
    return parsed


def read_ila_csv(path: Path) -> RawCapture:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    if not rows:
        raise AnalysisError("ILA CSV is empty")

    header_row, index_column, otr_column, data_slices = _find_header(rows)
    header = rows[header_row]
    max_column = max(
        [index_column, otr_column]
        + [data_slice.column for data_slice in data_slices]
    )
    otr_radix = _radix_for_column(rows, header_row, otr_column)
    data_radices = [
        _radix_for_column(rows, header_row, data_slice.column)
        for data_slice in data_slices
    ]

    indices: list[int] = []
    words: list[int] = []
    otr_values: list[int] = []
    for line_number, row in enumerate(rows[header_row + 1 :], start=header_row + 2):
        if not row or all(not value.strip() for value in row):
            continue
        if _normalized_header(row[0]).startswith("radix"):
            continue
        if len(row) <= max_column:
            raise AnalysisError(f"ILA CSV row {line_number} has too few columns")
        try:
            sample_index = int(row[index_column].strip(), 10)
        except ValueError as exc:
            raise AnalysisError(
                f"ILA CSV row {line_number} has invalid sample index: {row[index_column]!r}"
            ) from exc

        word = 0
        for data_slice, radix in zip(data_slices, data_radices, strict=True):
            slice_value = _parse_integer(
                row[data_slice.column],
                radix=radix,
                width=len(data_slice.logical_bits),
                label=f"row {line_number} ADC slice {header[data_slice.column]!r}",
            )
            for slice_bit, logical_bit in enumerate(data_slice.logical_bits):
                word |= ((slice_value >> slice_bit) & 1) << logical_bit
        otr = _parse_integer(
            row[otr_column],
            radix=otr_radix,
            width=1,
            label=f"row {line_number} OTR",
        )
        indices.append(sample_index)
        words.append(word)
        otr_values.append(otr)

    if len(indices) < MIN_CAPTURE_SAMPLES:
        raise AnalysisError(
            f"ILA CSV has {len(indices)} samples; at least {MIN_CAPTURE_SAMPLES} required"
        )
    index_array = np.asarray(indices, dtype=np.int64)
    if np.any(np.diff(index_array) != 1):
        raise AnalysisError("ILA sample indices are not strictly consecutive")
    return RawCapture(
        indices=index_array,
        words=np.asarray(words, dtype=np.uint16),
        otr=np.asarray(otr_values, dtype=np.uint8),
        data_column=" + ".join(header[item.column] for item in data_slices),
        otr_column=header[otr_column],
    )


def candidate_mappings(
    inferred_mapping: tuple[int, ...] | None = None,
) -> list[tuple[str, tuple[int, ...]]]:
    identity = tuple(range(ADC_BITS))
    candidates: dict[tuple[int, ...], str] = {identity: "identity"}
    reverse = tuple(reversed(identity))
    candidates[reverse] = "full_reverse"

    pairs = list(itertools.combinations(range(ADC_BITS), 2))
    for first, second in pairs:
        mapping = list(identity)
        mapping[first], mapping[second] = mapping[second], mapping[first]
        candidates.setdefault(tuple(mapping), f"swap_{first}_{second}")

    for pair_index, (a, b) in enumerate(pairs):
        for c, d in pairs[pair_index + 1 :]:
            if len({a, b, c, d}) != 4:
                continue
            mapping = list(identity)
            mapping[a], mapping[b] = mapping[b], mapping[a]
            mapping[c], mapping[d] = mapping[d], mapping[c]
            candidates.setdefault(tuple(mapping), f"swap_{a}_{b}__{c}_{d}")

    for start in range(ADC_BITS):
        for stop in range(start + 2, ADC_BITS + 1):
            mapping = list(identity)
            mapping[start:stop] = reversed(mapping[start:stop])
            candidates.setdefault(tuple(mapping), f"reverse_{start}_{stop - 1}")

    if inferred_mapping is not None:
        if len(inferred_mapping) != ADC_BITS or set(inferred_mapping) != set(identity):
            raise AnalysisError("inferred ADC mapping is not a 12-bit permutation")
        candidates.setdefault(inferred_mapping, "inferred_arbitrary_permutation")
        # Keep the existing runner-up gate meaningful even when the inferred
        # mapping is far from the identity-oriented diagnostic candidates.
        for first, second in itertools.combinations(range(ADC_BITS), 2):
            mapping = list(inferred_mapping)
            mapping[first], mapping[second] = mapping[second], mapping[first]
            candidates.setdefault(
                tuple(mapping),
                f"inferred_neighbor_swap_{first}_{second}",
            )
    return [(name, mapping) for mapping, name in candidates.items()]


def _fundamental_reference(
    words: np.ndarray,
    *,
    stimulus: str,
    frequency_hz: float,
    sample_rate_hz: float,
) -> tuple[np.ndarray, float, int]:
    """Build a mapping-independent analog-order reference from raw bit planes."""

    sample_count = int(words.size)
    angles = (
        2.0
        * math.pi
        * frequency_hz
        * np.arange(sample_count, dtype=np.float64)
        / sample_rate_hz
    )
    bits = ((words[:, None] >> np.arange(ADC_BITS)) & 1).astype(np.float64)
    basis = np.column_stack(
        (np.ones(sample_count), np.sin(angles), np.cos(angles))
    )
    coefficients = np.linalg.pinv(basis) @ bits
    amplitudes = np.hypot(coefficients[1], coefficients[2])
    anchor_bit = int(np.argmax(amplitudes))
    anchor_amplitude = float(amplitudes[anchor_bit])
    if not math.isfinite(anchor_amplitude) or anchor_amplitude <= 1e-9:
        raise AnalysisError("raw capture has no usable stimulus fundamental")
    phase = math.atan2(
        float(coefficients[2, anchor_bit]),
        float(coefficients[1, anchor_bit]),
    )
    if stimulus == "sine":
        reference = np.sin(angles + phase)
    elif stimulus == "triangle":
        reference = (2.0 / math.pi) * np.arcsin(np.sin(angles + phase))
    else:
        raise AnalysisError(f"unsupported stimulus {stimulus!r}")
    return reference, phase, anchor_bit


def _infer_arbitrary_mapping(
    words: np.ndarray,
    *,
    stimulus: str,
    frequency_hz: float,
    sample_rate_hz: float,
) -> tuple[tuple[int, ...], dict[str, Any]]:
    """Find the globally best physical-bit significance order.

    For an ordered sample pair, its most-significant differing bit decides the
    decoded order. Once a more-significant prefix is fixed, choosing the next
    bit has a prefix-only score, so a 2**12-state subset DP searches all 12!
    permutations exactly.
    """

    reference, phase, anchor_bit = _fundamental_reference(
        words,
        stimulus=stimulus,
        frequency_hz=frequency_hz,
        sample_rate_hz=sample_rate_hz,
    )
    difference_parts: list[np.ndarray] = []
    rising_parts: list[np.ndarray] = []
    for lag in ORDER_COMPARISON_LAGS:
        if lag >= words.size:
            continue
        reference_delta = reference[lag:] - reference[:-lag]
        selected = np.abs(reference_delta) > MIN_REFERENCE_DELTA
        if not np.any(selected):
            continue
        earlier = words[:-lag][selected]
        later = words[lag:][selected]
        increasing = reference_delta[selected] > 0.0
        lower_words = np.where(increasing, earlier, later).astype(np.uint16)
        upper_words = np.where(increasing, later, earlier).astype(np.uint16)
        differences = (lower_words ^ upper_words).astype(np.int64)
        rising_bits = ((~lower_words) & upper_words & ADC_MASK).astype(np.int64)
        informative = differences != 0
        difference_parts.append(differences[informative])
        rising_parts.append(rising_bits[informative])

    if not difference_parts:
        raise AnalysisError("raw capture has no informative ordered sample pairs")
    differences = np.concatenate(difference_parts)
    rising_bits = np.concatenate(rising_parts)
    if differences.size < MIN_CAPTURE_SAMPLES:
        raise AnalysisError("raw capture has too few informative ordered sample pairs")

    # counts[mask, bit] records 0->1 comparisons for a complete differing-bit
    # mask. Subset sums below turn these into a score for every MSB prefix.
    counts = np.zeros((1 << ADC_BITS, ADC_BITS), dtype=np.int64)
    for bit in range(ADC_BITS):
        np.add.at(counts[:, bit], differences, (rising_bits >> bit) & 1)

    gains = np.zeros_like(counts)
    complement = np.arange(1 << ADC_BITS, dtype=np.int64) ^ ADC_MASK
    for bit in range(ADC_BITS):
        subset_sums = counts[:, bit].copy()
        for subset_bit in range(ADC_BITS):
            stride = 1 << subset_bit
            for start in range(0, 1 << ADC_BITS, 2 * stride):
                subset_sums[start + stride : start + 2 * stride] += subset_sums[
                    start : start + stride
                ]
        gains[:, bit] = subset_sums[complement]

    best_scores = [-1] * (1 << ADC_BITS)
    best_orders: list[tuple[int, ...] | None] = [None] * (1 << ADC_BITS)
    best_scores[0] = 0
    best_orders[0] = ()
    for prefix in range(1 << ADC_BITS):
        order = best_orders[prefix]
        if order is None:
            continue
        for physical_bit in range(ADC_BITS):
            bit_mask = 1 << physical_bit
            if prefix & bit_mask:
                continue
            next_prefix = prefix | bit_mask
            score = best_scores[prefix] + int(gains[prefix, physical_bit])
            next_order = order + (physical_bit,)
            current_order = best_orders[next_prefix]
            if score > best_scores[next_prefix] or (
                score == best_scores[next_prefix]
                and (current_order is None or next_order < current_order)
            ):
                best_scores[next_prefix] = score
                best_orders[next_prefix] = next_order

    msb_to_lsb = best_orders[ADC_MASK]
    if msb_to_lsb is None:
        raise AnalysisError("arbitrary permutation search produced no complete order")
    physical_to_logical = [0] * ADC_BITS
    for significance_index, physical_bit in enumerate(msb_to_lsb):
        physical_to_logical[physical_bit] = ADC_BITS - 1 - significance_index
    comparison_accuracy = best_scores[ADC_MASK] / int(differences.size)
    return tuple(physical_to_logical), {
        "method": "exact_monotonic_subset_dp",
        "informative_comparison_count": int(differences.size),
        "comparison_accuracy": float(comparison_accuracy),
        "reference_anchor_physical_bit": anchor_bit,
        "reference_phase_rad": phase,
        "msb_to_lsb_physical_bits": list(msb_to_lsb),
    }


def _triangle_basis(sample_count: int, frequency_hz: float, sample_rate_hz: float, phase: float) -> np.ndarray:
    angles = (
        2.0 * math.pi * frequency_hz * np.arange(sample_count, dtype=np.float64)
        / sample_rate_hz
        + phase
    )
    return (2.0 / math.pi) * np.arcsin(np.sin(angles))


def _model_matrix(
    words: np.ndarray,
    *,
    stimulus: str,
    frequency_hz: float,
    sample_rate_hz: float,
) -> tuple[np.ndarray, float | None]:
    sample_count = int(words.size)
    times = np.arange(sample_count, dtype=np.float64) / sample_rate_hz
    if stimulus == "sine":
        angle = 2.0 * math.pi * frequency_hz * times
        return np.column_stack((np.ones(sample_count), np.sin(angle), np.cos(angle))), None
    if stimulus == "triangle":
        _reference, best_phase, _anchor_bit = _fundamental_reference(
            words,
            stimulus=stimulus,
            frequency_hz=frequency_hz,
            sample_rate_hz=sample_rate_hz,
        )
        basis = _triangle_basis(sample_count, frequency_hz, sample_rate_hz, best_phase)
        return np.column_stack((np.ones(sample_count), basis)), best_phase
    raise AnalysisError(f"unsupported stimulus {stimulus!r}")


def _score_candidates(
    words: np.ndarray,
    model: np.ndarray,
    candidates: Iterable[tuple[str, tuple[int, ...]]],
) -> list[dict[str, Any]]:
    bits = ((words[:, None] >> np.arange(ADC_BITS)) & 1).astype(np.float64)
    q_matrix, _ = np.linalg.qr(model, mode="reduced")
    residual_bits = bits - q_matrix @ (q_matrix.T @ bits)
    centered_bits = bits - np.mean(bits, axis=0, keepdims=True)
    residual_gram = residual_bits.T @ residual_bits
    centered_gram = centered_bits.T @ centered_bits
    coefficient_map = np.linalg.pinv(model) @ bits

    scores: list[dict[str, Any]] = []
    for name, mapping in candidates:
        weights = np.asarray([1 << logical_bit for logical_bit in mapping], dtype=np.float64)
        sse = max(0.0, float(weights @ residual_gram @ weights))
        sst = max(0.0, float(weights @ centered_gram @ weights))
        r_squared = 1.0 - sse / sst if sst > 0.0 else -math.inf
        coefficients = coefficient_map @ weights
        coefficients[0] -= 2048.0
        if model.shape[1] == 3:
            fitted_peak = math.hypot(float(coefficients[1]), float(coefficients[2]))
        else:
            fitted_peak = abs(float(coefficients[1]))
        fitted_pp = 2.0 * fitted_peak
        nrmse = math.sqrt(sse / words.size) / fitted_pp if fitted_pp > 0.0 else math.inf
        scores.append(
            {
                "name": name,
                "mapping_physical_to_logical": list(mapping),
                "r_squared": r_squared,
                "nrmse_fraction": nrmse,
                "fitted_peak_codes": fitted_peak,
                "fitted_pp_codes": fitted_pp,
                "coefficients": [float(value) for value in coefficients],
            }
        )
    scores.sort(key=lambda item: (item["r_squared"], -item["nrmse_fraction"]), reverse=True)
    return scores


def _decode(words: np.ndarray, mapping: list[int]) -> np.ndarray:
    bits = ((words[:, None] >> np.arange(ADC_BITS)) & 1).astype(np.int64)
    weights = np.asarray([1 << logical_bit for logical_bit in mapping], dtype=np.int64)
    return bits @ weights - 2048


def _window_winners(
    words: np.ndarray,
    *,
    stimulus: str,
    frequency_hz: float,
    sample_rate_hz: float,
    candidates: list[tuple[str, tuple[int, ...]]],
) -> list[list[int]]:
    winners: list[list[int]] = []
    for window in np.array_split(words, 4):
        model, _ = _model_matrix(
            window,
            stimulus=stimulus,
            frequency_hz=frequency_hz,
            sample_rate_hz=sample_rate_hz,
        )
        scores = _score_candidates(window, model, candidates)
        winners.append(scores[0]["mapping_physical_to_logical"])
    return winners


def analyze_capture(
    capture: RawCapture,
    *,
    stimulus: str,
    frequency_hz: float,
    sample_rate_hz: float = SAMPLE_RATE_HZ,
) -> dict[str, Any]:
    if not math.isfinite(frequency_hz) or frequency_hz <= 0.0:
        raise AnalysisError("frequency_hz must be positive and finite")
    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0.0:
        raise AnalysisError("sample_rate_hz must be positive and finite")
    if frequency_hz >= sample_rate_hz / 2.0:
        raise AnalysisError("stimulus frequency must be below the raw Nyquist frequency")

    words = capture.words
    bit_values = ((words[:, None] >> np.arange(ADC_BITS)) & 1).astype(np.uint8)
    ones = np.sum(bit_values, axis=0)
    zeros = words.size - ones
    toggles = np.sum(bit_values[1:] != bit_values[:-1], axis=0)
    identifiable = (ones >= MIN_BIT_STATE_COUNT) & (zeros >= MIN_BIT_STATE_COUNT)
    identifiable_mask = sum((1 << bit) for bit, value in enumerate(identifiable) if value)

    model, triangle_phase = _model_matrix(
        words,
        stimulus=stimulus,
        frequency_hz=frequency_hz,
        sample_rate_hz=sample_rate_hz,
    )
    inferred_mapping: tuple[int, ...] | None = None
    permutation_search: dict[str, Any]
    try:
        inferred_mapping, permutation_search = _infer_arbitrary_mapping(
            words,
            stimulus=stimulus,
            frequency_hz=frequency_hz,
            sample_rate_hz=sample_rate_hz,
        )
        permutation_search["available"] = True
    except AnalysisError as exc:
        permutation_search = {
            "available": False,
            "method": "exact_monotonic_subset_dp",
            "failure": str(exc),
        }
    candidates = candidate_mappings(inferred_mapping)
    scores = _score_candidates(words, model, candidates)
    winner = scores[0]
    runner_up = scores[1]
    runner_up_gap = float(winner["r_squared"] - runner_up["r_squared"])
    runner_up_nrmse_ratio = (
        float(runner_up["nrmse_fraction"] / winner["nrmse_fraction"])
        if winner["nrmse_fraction"] > 0.0
        else math.inf
    )
    decoded = _decode(words, winner["mapping_physical_to_logical"])
    coefficients = np.asarray(winner["coefficients"], dtype=np.float64)
    fitted = model @ coefficients
    residual = decoded.astype(np.float64) - fitted
    fitted_span = max(float(winner["fitted_pp_codes"]), 1.0)
    outlier_threshold = max(8.0, 0.01 * fitted_span)
    outlier_mask = np.abs(residual) > outlier_threshold
    outlier_count = int(np.count_nonzero(outlier_mask))
    outlier_rate = float(np.mean(outlier_mask))
    residual_max_abs = float(np.max(np.abs(residual)))
    residual_step_max_abs = (
        float(np.max(np.abs(np.diff(residual)))) if residual.size > 1 else 0.0
    )
    triangle_code_step = (
        2.0 * float(winner["fitted_pp_codes"]) * frequency_hz / sample_rate_hz
        if stimulus == "triangle"
        else None
    )

    period_samples = sample_rate_hz / frequency_hz
    period_lag = int(round(period_samples))
    repeatability_p99 = None
    repeatability_max = None
    if (
        abs(period_samples - period_lag) <= 1e-9
        and period_lag > 0
        and words.size >= 2 * period_lag
    ):
        repeatability_delta = np.abs(
            decoded[period_lag:] - decoded[:-period_lag]
        )
        repeatability_p99 = float(np.percentile(repeatability_delta, 99.0))
        repeatability_max = float(np.max(repeatability_delta))

    window_winners = _window_winners(
        words,
        stimulus=stimulus,
        frequency_hz=frequency_hz,
        sample_rate_hz=sample_rate_hz,
        candidates=candidates,
    )
    mapping_static = all(
        mapping == winner["mapping_physical_to_logical"] for mapping in window_winners
    )
    otr_count = int(np.count_nonzero(capture.otr))

    gates = {
        "otr_zero": otr_count == 0,
        "arbitrary_permutation_search_available": bool(
            permutation_search["available"]
        ),
        "order_comparison_accuracy_at_least_0_99": bool(
            permutation_search.get("comparison_accuracy", -math.inf)
            >= MIN_ORDER_COMPARISON_ACCURACY
        ),
        "triangle_code_step_at_most_2_5": bool(
            triangle_code_step is None
            or triangle_code_step <= MAX_TRIANGLE_CODE_STEP_PER_SAMPLE
        ),
        "all_bits_observed_both_states": identifiable_mask == ADC_MASK,
        "r_squared_at_least_0_995": winner["r_squared"] >= MIN_R_SQUARED,
        "nrmse_at_most_0_02": winner["nrmse_fraction"] <= MAX_NRMSE_FRACTION,
        "runner_up_nrmse_ratio_at_least_1_5": (
            runner_up_nrmse_ratio >= MIN_RUNNER_UP_NRMSE_RATIO
        ),
        "outlier_rate_at_most_0_001": outlier_rate <= MAX_OUTLIER_RATE,
        "catastrophic_outlier_count_zero": (
            outlier_count <= MAX_CATASTROPHIC_OUTLIER_COUNT
        ),
        "residual_step_at_most_16_codes": (
            residual_step_max_abs <= MAX_RESIDUAL_STEP_CODES
        ),
        "cycle_repeatability_max_at_most_8_codes": (
            repeatability_max is None
            or repeatability_max <= MAX_CYCLE_REPEATABILITY_CODES
        ),
        "mapping_static_across_four_windows": mapping_static,
    }
    freeze_allowed = all(gates.values())
    mapping = winner["mapping_physical_to_logical"]
    if freeze_allowed and mapping == list(range(ADC_BITS)):
        classification = "direct"
        recommended_action = "keep_direct_mapping"
    elif freeze_allowed and mapping == list(reversed(range(ADC_BITS))):
        classification = "full_reverse"
        recommended_action = "use_full_reverse_only_after_independent_capture"
    elif freeze_allowed:
        classification = "local_permutation"
        recommended_action = "implement_explicit_permutation_and_retest"
    elif (
        not gates["catastrophic_outlier_count_zero"]
        or not gates["residual_step_at_most_16_codes"]
        or not gates["cycle_repeatability_max_at_most_8_codes"]
    ):
        classification = "unstable_nonstatic_mapping"
        recommended_action = "compare_full_cycle_diagnostic_phase_captures"
    elif not gates["all_bits_observed_both_states"] or not gates[
        "runner_up_nrmse_ratio_at_least_1_5"
    ]:
        classification = "insufficient_evidence"
        recommended_action = "increase_safe_code_coverage_or_add_dc_levels"
    else:
        classification = "unstable_nonstatic_mapping"
        recommended_action = "compare_full_cycle_diagnostic_phase_captures"

    failures = [name for name, passed in gates.items() if not passed]
    return {
        "format": "CycleScope raw IOB bit-order analysis v1",
        "pass": freeze_allowed,
        "freeze_allowed": freeze_allowed,
        "classification": classification,
        "failures": failures,
        "warnings": [],
        "acquisition": {
            "sample_rate_hz": sample_rate_hz,
            "sample_count": int(words.size),
            "first_sample_index": int(capture.indices[0]),
            "last_sample_index": int(capture.indices[-1]),
            "data_column": capture.data_column,
            "otr_column": capture.otr_column,
        },
        "stimulus": {
            "type": stimulus,
            "frequency_hz": frequency_hz,
            "triangle_phase_rad": triangle_phase,
        },
        "raw_quality": {
            "unique_words": int(np.unique(words).size),
            "raw_min": int(np.min(words)),
            "raw_max": int(np.max(words)),
            "otr_count": otr_count,
            "identifiable_bits_mask": f"0x{identifiable_mask:03x}",
            "per_physical_bit": [
                {
                    "bit": bit,
                    "ones_count": int(ones[bit]),
                    "zeros_count": int(zeros[bit]),
                    "ones_fraction": float(ones[bit] / words.size),
                    "toggle_count": int(toggles[bit]),
                    "observed_both_states": bool(identifiable[bit]),
                }
                for bit in range(ADC_BITS)
            ],
        },
        "winner": {
            **winner,
            "runner_up_gap": runner_up_gap,
            "runner_up_nrmse_ratio": runner_up_nrmse_ratio,
            "outlier_count": outlier_count,
            "outlier_rate": outlier_rate,
            "outlier_threshold_codes": outlier_threshold,
            "residual_max_abs_codes": residual_max_abs,
            "residual_step_max_abs_codes": residual_step_max_abs,
            "triangle_fitted_code_step_per_sample": triangle_code_step,
            "cycle_repeatability_p99_codes": repeatability_p99,
            "cycle_repeatability_max_codes": repeatability_max,
            "window_winners_physical_to_logical": window_winners,
            "encoding": "offset_binary",
            "polarity": "not_identifiable_without_synchronized_analog_reference",
        },
        "top_candidates": scores[:10],
        "permutation_search": permutation_search,
        "gates": gates,
        "recommended_action": recommended_action,
    }


def load_and_validate_capture_manifest(path: Path, csv_path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisError(f"invalid capture manifest {path}: {exc}") from exc
    if manifest.get("format") != "CycleScope raw IOB ILA capture manifest v1":
        raise AnalysisError("unexpected capture manifest format")
    csv_binding = manifest.get("csv")
    if not isinstance(csv_binding, dict):
        raise AnalysisError("capture manifest has no CSV binding")
    if csv_binding.get("file") != csv_path.name:
        raise AnalysisError("capture manifest CSV filename mismatch")
    actual_sha256 = sha256_file(csv_path)
    if csv_binding.get("sha256") != actual_sha256:
        raise AnalysisError("capture manifest CSV SHA-256 mismatch")
    if manifest.get("sample_rate_hz") != int(SAMPLE_RATE_HZ):
        raise AnalysisError("capture manifest sample rate is not 65 MHz")
    if manifest.get("capture_depth") != 16_384:
        raise AnalysisError("capture manifest depth is not 16384")
    sample_phase = manifest.get("sample_phase_deg")
    if type(sample_phase) is not int or sample_phase not in SUPPORTED_SAMPLE_PHASES:
        raise AnalysisError("capture manifest sample phase is unsupported")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="Vivado ILA CSV export")
    parser.add_argument(
        "--capture-manifest", type=Path, required=True, help="hash-bound capture_manifest.json"
    )
    parser.add_argument("--stimulus", choices=("sine", "triangle"), required=True)
    parser.add_argument("--frequency-hz", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    csv_path = args.csv.resolve()
    manifest_path = args.capture_manifest.resolve()
    output_path = args.output.resolve()
    if output_path.exists():
        raise AnalysisError(f"refusing to overwrite analysis output: {output_path}")
    if not output_path.parent.is_dir():
        raise AnalysisError(f"analysis output parent does not exist: {output_path.parent}")

    manifest = load_and_validate_capture_manifest(manifest_path, csv_path)
    capture = read_ila_csv(csv_path)
    if capture.words.size != manifest["capture_depth"]:
        raise AnalysisError(
            f"capture sample count {capture.words.size} does not match manifest depth"
        )
    result = analyze_capture(
        capture,
        stimulus=args.stimulus,
        frequency_hz=args.frequency_hz,
        sample_rate_hz=float(manifest["sample_rate_hz"]),
    )
    result["input_binding"] = {
        "csv": {"path": str(csv_path), "sha256": sha256_file(csv_path)},
        "capture_manifest": {
            "path": str(manifest_path),
            "sha256": sha256_file(manifest_path),
        },
        "bitstream_sha256": manifest["bitstream_sha256"],
        "ltx_sha256": manifest["ltx_sha256"],
        "sample_phase_deg": manifest["sample_phase_deg"],
    }
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"RAW_IOB_ANALYSIS_CLASSIFICATION={result['classification']}")
    print(f"RAW_IOB_ANALYSIS_FREEZE_ALLOWED={int(result['freeze_allowed'])}")
    print(f"RAW_IOB_ANALYSIS_OUTPUT={output_path}")
    return 0 if result["freeze_allowed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
