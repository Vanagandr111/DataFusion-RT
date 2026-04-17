from __future__ import annotations

import math


def simulated_scale_mass(elapsed_s: float) -> float:
    baseline = max(35.0, 125.0 - elapsed_s * 0.045)
    ripple = math.sin(elapsed_s / 7.0) * 0.45 + math.sin(elapsed_s / 1.8) * 0.12
    return round(baseline + ripple, 3)


def simulated_furnace_pv(elapsed_s: float) -> float:
    base = min(650.0, 25.0 + elapsed_s * 2.4)
    return round(base + math.sin(elapsed_s / 6.0) * 3.0, 2)


def simulated_furnace_sv(elapsed_s: float) -> float:
    return 650.0 if elapsed_s >= 30 else 400.0
