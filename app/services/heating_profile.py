from __future__ import annotations

import math
from datetime import datetime
from typing import Sequence

from app.compat import dataclass


@dataclass(slots=True)
class HeatingProfileResult:
    timestamps: list[datetime]
    temperatures: list[float]
    source_series: str
    breakpoints: tuple[int, ...]
    start_index: int | None = None
    peak_index: int | None = None
    cooldown_index: int | None = None
    stable_index: int | None = None
    base_temperature: float | None = None
    peak_temperature: float | None = None
    stable_temperature: float | None = None
    minimum_temperature: float | None = None

    @property
    def has_data(self) -> bool:
        return bool(self.timestamps and self.temperatures)

    @property
    def ready_for_furnace_summary(self) -> bool:
        return self.has_data and self.start_index is not None


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
        last_index = len(clean_temperatures) - 1
        last_value = clean_temperatures[last_index]
        return HeatingProfileResult(
            timestamps=clean_timestamps,
            temperatures=clean_temperatures,
            source_series=source_series,
            breakpoints=(0, last_index),
            start_index=0,
            peak_index=last_index,
            cooldown_index=last_index,
            stable_index=last_index,
            base_temperature=clean_temperatures[0],
            peak_temperature=last_value,
            stable_temperature=last_value,
            minimum_temperature=min(clean_temperatures),
        )

    median_window = _adaptive_window(len(clean_temperatures), minimum=5, preferred=7)
    average_window = _adaptive_window(len(clean_temperatures), minimum=7, preferred=11)
    smoothed = _moving_average(_moving_median(clean_temperatures, median_window), average_window)
    analysis = _analyze_heating_segments(time_seconds, smoothed)
    anchor_indices = _compress_breakpoints(
        [
            0,
            analysis["start_index"],
            analysis["stable_index"] if analysis["stable_index"] is not None else len(smoothed) - 1,
            len(smoothed) - 1,
        ],
        min_gap=max(2, len(smoothed) // 24),
    )
    profile_values = _build_reference_profile(smoothed, analysis, anchor_indices)
    return HeatingProfileResult(
        timestamps=clean_timestamps,
        temperatures=profile_values,
        source_series=source_series,
        breakpoints=tuple(anchor_indices),
        start_index=analysis["start_index"],
        peak_index=analysis["peak_index"],
        cooldown_index=analysis["cooldown_index"],
        stable_index=analysis["stable_index"],
        base_temperature=analysis["base_temperature"],
        peak_temperature=analysis["peak_temperature"],
        stable_temperature=analysis["stable_temperature"],
        minimum_temperature=min(clean_temperatures),
    )


def summarize_furnace_profile(result: HeatingProfileResult) -> dict[str, str]:
    if not result.has_data:
        return {
            "source": "идёт анализ",
            "heat_start": "идёт анализ",
            "time_to_peak": "идёт анализ",
            "cooldown_to_stable": "идёт анализ",
            "elapsed": "идёт анализ",
            "peak_temperature": "идёт анализ",
            "stable_temperature": "идёт анализ",
            "minimum_temperature": "идёт анализ",
            "overheat_above_stable": "идёт анализ",
        }
    timestamps = result.timestamps
    start_index = result.start_index if result.start_index is not None else 0
    peak_index = result.peak_index if result.peak_index is not None else len(timestamps) - 1
    stable_index = result.stable_index
    cooldown_index = result.cooldown_index
    start_time = timestamps[start_index]
    peak_time = timestamps[peak_index]
    end_time = timestamps[-1]
    stable_time = timestamps[stable_index] if stable_index is not None else None
    cooldown_time = timestamps[cooldown_index] if cooldown_index is not None else stable_time
    overheat = None
    if result.peak_temperature is not None and result.stable_temperature is not None:
        overheat = result.peak_temperature - result.stable_temperature
    return {
        "source": "Камера",
        "heat_start": start_time.strftime("%H:%M:%S"),
        "time_to_peak": _format_duration((peak_time - start_time).total_seconds()),
        "cooldown_to_stable": (
            _format_duration((stable_time - peak_time).total_seconds())
            if stable_time is not None
            else "идёт анализ"
        ),
        "elapsed": _format_duration((end_time - start_time).total_seconds()),
        "peak_temperature": _format_temp(result.peak_temperature),
        "stable_temperature": _format_temp(result.stable_temperature)
        if stable_time is not None
        else "идёт анализ",
        "minimum_temperature": _format_temp(result.minimum_temperature),
        "overheat_above_stable": _format_temp(overheat),
        "cooldown_start": cooldown_time.strftime("%H:%M:%S") if cooldown_time is not None else "идёт анализ",
        "stable_time": stable_time.strftime("%H:%M:%S") if stable_time is not None else "идёт анализ",
    }


def _build_reference_profile(
    smoothed: list[float],
    analysis: dict[str, int | float | None],
    anchor_indices: list[int],
) -> list[float]:
    base_value = float(analysis["base_temperature"] or smoothed[0])
    stable_value = float(
        analysis["stable_temperature"]
        if analysis["stable_temperature"] is not None
        else smoothed[-1]
    )
    profile_points: dict[int, float] = {}
    for index in anchor_indices:
        if index <= int(analysis["start_index"] or 0):
            profile_points[index] = base_value
        else:
            profile_points[index] = stable_value
    if anchor_indices:
        profile_points[anchor_indices[0]] = base_value
        profile_points[anchor_indices[-1]] = stable_value
    result: list[float] = []
    for index in range(len(smoothed)):
        result.append(_interpolate_profile_value(index, profile_points))
    return result


def _interpolate_profile_value(index: int, profile_points: dict[int, float]) -> float:
    anchors = sorted(profile_points)
    if not anchors:
        return math.nan
    if index <= anchors[0]:
        return profile_points[anchors[0]]
    for left, right in zip(anchors, anchors[1:]):
        if index <= right:
            if right <= left:
                return profile_points[right]
            ratio = (index - left) / (right - left)
            left_value = profile_points[left]
            right_value = profile_points[right]
            return left_value + (right_value - left_value) * ratio
    return profile_points[anchors[-1]]


def _analyze_heating_segments(
    time_seconds: list[float], temperatures: list[float]
) -> dict[str, int | float | None]:
    slopes = _first_derivative(time_seconds, temperatures)
    finite_slopes = [value for value in slopes if not math.isnan(value)]
    peak_slope = max(finite_slopes) if finite_slopes else 0.0
    temperature_span = max(temperatures) - min(temperatures)
    duration = max(1.0, time_seconds[-1] - time_seconds[0])
    baseline_slope = temperature_span / duration if duration > 0 else 0.0
    start_threshold = max(peak_slope * 0.24, baseline_slope * 0.55, 0.01)
    rise_threshold = max(temperature_span * 0.03, 0.6)
    base_temperature = temperatures[0]
    start_index = 0
    for index, (temperature, slope) in enumerate(zip(temperatures, slopes)):
        if temperature >= base_temperature + rise_threshold or slope >= start_threshold:
            start_index = index
            break

    peak_index = max(range(len(temperatures)), key=lambda idx: temperatures[idx])
    peak_temperature = temperatures[peak_index]
    stable_window = max(4, len(temperatures) // 18)
    stable_band = max(1.5, temperature_span * 0.02)
    slope_band = max(peak_slope * 0.08, baseline_slope * 0.1, 0.01)

    tail_start = max(peak_index + 1, len(temperatures) - stable_window * 2)
    tail = temperatures[tail_start:] if tail_start < len(temperatures) else temperatures[-stable_window:]
    stable_temperature = sum(tail) / len(tail) if tail else temperatures[-1]

    confirmed_stable_index: int | None = None
    for index in range(max(start_index + 1, peak_index), len(temperatures) - stable_window + 1):
        chunk = temperatures[index : index + stable_window]
        chunk_slopes = [abs(value) for value in slopes[index : index + stable_window] if not math.isnan(value)]
        if not chunk or not chunk_slopes:
            continue
        if (max(chunk) - min(chunk)) <= stable_band and max(chunk_slopes) <= slope_band:
            confirmed_stable_index = index
            break

    cooldown_index: int | None = None
    if peak_index < len(temperatures) - 1:
        for index in range(peak_index, len(temperatures) - 1):
            if temperatures[index + 1] < temperatures[index]:
                cooldown_index = index + 1
                break
    if cooldown_index is None:
        cooldown_index = confirmed_stable_index

    entry_band = max(stable_band * 1.8, 3.0)
    stable_index: int | None = None
    if cooldown_index is not None:
        search_end = confirmed_stable_index if confirmed_stable_index is not None else len(temperatures) - 1
        for index in range(cooldown_index, search_end + 1):
            temperature = temperatures[index]
            slope = slopes[index] if index < len(slopes) else math.nan
            if (
                temperature <= stable_temperature + entry_band
                and temperature >= stable_temperature - stable_band
                and (math.isnan(slope) or slope <= slope_band)
            ):
                stable_index = index
                break
    if stable_index is None:
        stable_index = confirmed_stable_index
    if stable_index is None and cooldown_index is not None:
        stable_index = cooldown_index
    if stable_index is None:
        stable_index = len(temperatures) - 1

    return {
        "start_index": start_index,
        "peak_index": peak_index,
        "cooldown_index": cooldown_index,
        "stable_index": stable_index,
        "base_temperature": base_temperature,
        "peak_temperature": peak_temperature,
        "stable_temperature": stable_temperature,
    }


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _format_temp(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "идёт анализ"
    return f"{value:.1f} °C"


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
