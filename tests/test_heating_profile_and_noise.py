from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from app.services.heating_profile import build_heating_profile, summarize_furnace_profile
from app.services.noise_filter import NoiseReductionConfig, apply_noise_reduction


class HeatingProfileAndNoiseTests(unittest.TestCase):
    def test_heating_profile_prefers_stable_ramp_and_summary(self) -> None:
        start = datetime(2026, 1, 1, 12, 0, 0)
        timestamps = [start + timedelta(seconds=30 * index) for index in range(12)]
        temperatures = [25, 28, 35, 55, 80, 120, 145, 170, 158, 152, 150, 149]
        result = build_heating_profile(timestamps, temperatures, source_series="temperature")
        summary = summarize_furnace_profile(result)
        self.assertTrue(result.has_data)
        self.assertEqual(result.source_series, "temperature")
        self.assertIsNotNone(result.peak_index)
        self.assertEqual(summary["source"], "Камера")
        self.assertIn("°C", summary["peak_temperature"])

    def test_noise_reduction_cuts_single_spike(self) -> None:
        values = [0.0, 0.1, 0.2, 5.0, 0.21, 0.2]
        filtered = apply_noise_reduction(
            values,
            NoiseReductionConfig(
                enabled=True,
                median_window=3,
                spike_threshold=1.0,
                step_threshold=0.0,
            ),
        )
        self.assertLess(filtered[3], 1.0)


if __name__ == "__main__":
    unittest.main()
