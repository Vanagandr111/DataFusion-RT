from __future__ import annotations

import math
import statistics

from app.compat import dataclass


@dataclass(slots=True)
class NoiseReductionConfig:
    enabled: bool = False
    median_window: int = 5
    spike_threshold: float = 0.6
    step_threshold: float = 0.05
    target_series: str = "all"


def auto_noise_reduction(values: list[float]) -> NoiseReductionConfig:
    finite = [value for value in values if not math.isnan(value)]
    if len(finite) < 8:
        return NoiseReductionConfig(enabled=True, median_window=5, spike_threshold=0.6, step_threshold=0.05)
    deltas = [abs(finite[index] - finite[index - 1]) for index in range(1, len(finite))]
    median_delta = statistics.median(deltas) if deltas else 0.05
    high_delta = statistics.quantiles(deltas, n=4)[-1] if len(deltas) >= 4 else max(deltas, default=median_delta)
    return NoiseReductionConfig(
        enabled=True,
        median_window=5 if len(finite) < 40 else 7,
        spike_threshold=max(0.2, high_delta * 1.4),
        step_threshold=max(0.01, median_delta * 0.9),
    )


def apply_noise_reduction(values: list[float], config: NoiseReductionConfig) -> list[float]:
    if not config.enabled:
        return list(values)
    medianed = _moving_median(values, max(1, config.median_window))
    result: list[float] = []
    previous_kept = math.nan
    for raw, median in zip(values, medianed):
        if math.isnan(raw):
            result.append(math.nan)
            previous_kept = math.nan
            continue
        candidate = raw
        if not math.isnan(median) and abs(raw - median) > config.spike_threshold:
            candidate = median
        if not math.isnan(previous_kept) and abs(candidate - previous_kept) < config.step_threshold:
            candidate = previous_kept
        result.append(candidate)
        previous_kept = candidate
    return result


def _moving_median(values: list[float], window: int) -> list[float]:
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
