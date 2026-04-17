from __future__ import annotations

import math
from datetime import datetime
from typing import MutableSequence


def coerce_timestamp(raw_timestamp: str | datetime | None) -> datetime | None:
    if raw_timestamp is None:
        return None
    if isinstance(raw_timestamp, datetime):
        return raw_timestamp.replace(tzinfo=None) if raw_timestamp.tzinfo else raw_timestamp
    try:
        parsed = datetime.fromisoformat(raw_timestamp)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def smooth_values(values: list[float], window: int = 5) -> list[float]:
    if window <= 1:
        return values
    result: list[float] = []
    for index in range(len(values)):
        chunk = [value for value in values[max(0, index - window + 1): index + 1] if not math.isnan(value)]
        result.append(sum(chunk) / len(chunk) if chunk else math.nan)
    return result


def first_finite(values: list[float]) -> float | None:
    for value in values:
        if not math.isnan(value):
            return value
    return None


def last_finite(values: list[float]) -> float | None:
    for value in reversed(values):
        if not math.isnan(value):
            return value
    return None


def first_finite_index(values: list[float]) -> int | None:
    for index, value in enumerate(values):
        if not math.isnan(value):
            return index
    return None


def last_finite_index(values: list[float]) -> int | None:
    for index in range(len(values) - 1, -1, -1):
        if not math.isnan(values[index]):
            return index
    return None


def normalize_mass_values(values: list[float]) -> list[float]:
    baseline = first_finite(values)
    if baseline in {None, 0.0}:
        return values
    return [math.nan if math.isnan(value) else (value / baseline) * 100.0 for value in values]


def series_values(values: list[float], *, smooth: bool) -> list[float]:
    return smooth_values(values) if smooth else values


def mass_series_values(values: list[float], *, normalized: bool, smooth: bool) -> list[float]:
    result = list(values)
    if normalized:
        result = normalize_mass_values(result)
    return smooth_values(result) if smooth else result


def delta_values(values: list[float], *, smooth: bool) -> list[float]:
    result: list[float] = []
    previous: float | None = None
    for value in values:
        if math.isnan(value):
            result.append(math.nan)
            previous = None
            continue
        result.append(math.nan if previous is None else value - previous)
        previous = value
    return smooth_values(result) if smooth else result


def dtg_values(timestamps: list[datetime], values: list[float], *, smooth: bool) -> list[float]:
    result: list[float] = []
    previous_value: float | None = None
    previous_time: datetime | None = None
    for timestamp, value in zip(timestamps, values):
        if math.isnan(value):
            result.append(math.nan)
            previous_value = None
            previous_time = None
            continue
        if previous_value is None or previous_time is None:
            result.append(math.nan)
        else:
            delta_minutes = (timestamp - previous_time).total_seconds() / 60.0
            result.append(math.nan if delta_minutes <= 0 else (value - previous_value) / delta_minutes)
        previous_value = value
        previous_time = timestamp
    return smooth_values(result) if smooth else result


def append_series_sample(
    target_timestamps: MutableSequence[datetime],
    target_values: MutableSequence[float],
    raw_timestamp: str | None,
    raw_value: float | None,
) -> None:
    timestamp = coerce_timestamp(raw_timestamp)
    if timestamp is None:
        return
    if target_timestamps and target_timestamps[-1] == timestamp:
        return
    target_timestamps.append(timestamp)
    target_values.append(raw_value if raw_value is not None else math.nan)
