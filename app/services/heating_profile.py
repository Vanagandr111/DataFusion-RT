from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence


@dataclass(slots=True)
class HeatingProfileResult:
    timestamps: list[datetime]
    temperatures: list[float]
    source_series: str
    breakpoints: tuple[int, ...]

    @property
    def has_data(self) -> bool:
        return bool(self.timestamps and self.temperatures)


def build_heating_profile(
    timestamps: Sequence[datetime],
    temperatures: Sequence[float],
    *,
    source_series: str,
) -> HeatingProfileResult:
    cleaned = [
        (timestamp, float(value))
        for timestamp, value in zip(timestamps, temperatures)
        if value is not None and not math.isnan(float(value))
    ]
    if len(cleaned) < 6:
        return HeatingProfileResult([], [], source_series, ())

    clean_timestamps = [item[0] for item in cleaned]
    clean_temperatures = [item[1] for item in cleaned]
    time_seconds = _relative_seconds(clean_timestamps)
    if not time_seconds or time_seconds[-1] <= 0:
        return HeatingProfileResult(clean_timestamps, clean_temperatures, source_series, (0, len(clean_temperatures) - 1))

    median_window = _adaptive_window(len(clean_temperatures), minimum=5, preferred=7)
    average_window = _adaptive_window(len(clean_temperatures), minimum=7, preferred=11)
    median_smoothed = _moving_median(clean_temperatures, median_window)
    smoothed = _moving_average(median_smoothed, average_window)

    start_idx, ramp_start_idx, plateau_idx = _detect_breakpoints(time_seconds, smoothed)
    anchor_indices = _compress_breakpoints(
        [start_idx, ramp_start_idx, plateau_idx, len(smoothed) - 1],
        min_gap=max(2, len(smoothed) // 24),
    )
    profile_values = _piecewise_profile(time_seconds, smoothed, anchor_indices)
    return HeatingProfileResult(
        timestamps=clean_timestamps,
        temperatures=profile_values,
        source_series=source_series,
        breakpoints=tuple(anchor_indices),
    )


def _detect_breakpoints(time_seconds: list[float], temperatures: list[float]) -> tuple[int, int, int]:
    slopes = _first_derivative(time_seconds, temperatures)
    finite_slopes = [value for value in slopes if not math.isnan(value)]
    if not finite_slopes:
        end_idx = len(temperatures) - 1
        return 0, 0, end_idx

    peak_slope = max(finite_slopes)
    temperature_span = max(temperatures) - min(temperatures)
    duration = max(1.0, time_seconds[-1] - time_seconds[0])
    baseline_slope = temperature_span / duration if duration > 0 else 0.0
    start_threshold = max(peak_slope * 0.28, baseline_slope * 0.65, 0.01)
    plateau_threshold = max(peak_slope * 0.14, baseline_slope * 0.22, 0.004)
    rise_threshold = max(temperature_span * 0.035, 0.6)

    base_temperature = temperatures[0]
    ramp_start_idx = 0
    for index, (temperature, slope) in enumerate(zip(temperatures, slopes)):
        if temperature >= base_temperature + rise_threshold or slope >= start_threshold:
            ramp_start_idx = index
            break

    peak_idx = max(range(len(slopes)), key=lambda idx: slopes[idx] if not math.isnan(slopes[idx]) else float("-inf"))
    plateau_idx = len(temperatures) - 1
    plateau_window = max(3, len(temperatures) // 18)
    for index in range(max(ramp_start_idx + 1, peak_idx), len(slopes) - plateau_window):
        window = [abs(item) for item in slopes[index : index + plateau_window] if not math.isnan(item)]
        if not window:
            continue
        if max(window) <= plateau_threshold:
            plateau_idx = index
            break

    if plateau_idx <= ramp_start_idx:
        plateau_idx = max(ramp_start_idx + 1, min(len(temperatures) - 1, peak_idx))

    return 0, ramp_start_idx, plateau_idx


def _piecewise_profile(
    time_seconds: list[float],
    temperatures: list[float],
    anchor_indices: list[int],
) -> list[float]:
    if len(anchor_indices) < 2:
        return temperatures[:]
    result: list[float] = []
    for second in time_seconds:
        result.append(_interpolate_by_anchors(second, time_seconds, temperatures, anchor_indices))
    return result


def _interpolate_by_anchors(
    second: float,
    time_seconds: list[float],
    temperatures: list[float],
    anchor_indices: list[int],
) -> float:
    if second <= time_seconds[anchor_indices[0]]:
        return temperatures[anchor_indices[0]]
    for left_idx, right_idx in zip(anchor_indices, anchor_indices[1:]):
        left_time = time_seconds[left_idx]
        right_time = time_seconds[right_idx]
        if second <= right_time:
            if right_time <= left_time:
                return temperatures[right_idx]
            ratio = (second - left_time) / (right_time - left_time)
            return temperatures[left_idx] + (temperatures[right_idx] - temperatures[left_idx]) * ratio
    return temperatures[anchor_indices[-1]]


def _relative_seconds(timestamps: Sequence[datetime]) -> list[float]:
    if not timestamps:
        return []
    start = timestamps[0]
    return [(timestamp - start).total_seconds() for timestamp in timestamps]


def _first_derivative(time_seconds: Sequence[float], values: Sequence[float]) -> list[float]:
    if len(values) < 2:
        return [math.nan for _ in values]
    result: list[float] = [math.nan]
    for index in range(1, len(values)):
        delta_time = time_seconds[index] - time_seconds[index - 1]
        if delta_time <= 0:
            result.append(math.nan)
            continue
        result.append((values[index] - values[index - 1]) / delta_time)
    return result


def _moving_average(values: Sequence[float], window: int) -> list[float]:
    if window <= 1:
        return list(values)
    radius = window // 2
    result: list[float] = []
    for index in range(len(values)):
        chunk = [
            values[pos]
            for pos in range(max(0, index - radius), min(len(values), index + radius + 1))
            if not math.isnan(values[pos])
        ]
        result.append(sum(chunk) / len(chunk) if chunk else math.nan)
    return result


def _moving_median(values: Sequence[float], window: int) -> list[float]:
    if window <= 1:
        return list(values)
    radius = window // 2
    result: list[float] = []
    for index in range(len(values)):
        chunk = sorted(
            values[pos]
            for pos in range(max(0, index - radius), min(len(values), index + radius + 1))
            if not math.isnan(values[pos])
        )
        if not chunk:
            result.append(math.nan)
            continue
        middle = len(chunk) // 2
        if len(chunk) % 2:
            result.append(chunk[middle])
        else:
            result.append((chunk[middle - 1] + chunk[middle]) / 2.0)
    return result


def _adaptive_window(length: int, *, minimum: int, preferred: int) -> int:
    window = min(preferred, length if length % 2 else length - 1)
    if window < minimum:
        window = minimum
    if window % 2 == 0:
        window += 1
    return max(1, min(window, length if length % 2 else max(1, length - 1)))


def _compress_breakpoints(indices: list[int], *, min_gap: int) -> list[int]:
    cleaned: list[int] = []
    for index in indices:
        if not cleaned:
            cleaned.append(index)
            continue
        if index - cleaned[-1] < min_gap:
            cleaned[-1] = max(cleaned[-1], index)
            continue
        cleaned.append(index)
    if len(cleaned) == 1:
        cleaned.append(cleaned[0])
    return cleaned
