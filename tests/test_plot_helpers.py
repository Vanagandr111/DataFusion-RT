import unittest
from collections import deque
from datetime import datetime, timedelta

from app.services.plot_series_helpers import (
    append_series_sample,
    dtg_values,
    mass_series_values,
)
from app.services.plot_style_helpers import sanitize_series_style


class PlotHelperTests(unittest.TestCase):
    def test_sanitize_series_style_normalizes_values(self) -> None:
        style = sanitize_series_style(
            {"linestyle": "dashed", "linewidth": "5.2", "color": " #123456 "}
        )
        self.assertEqual(style["linestyle"], "--")
        self.assertEqual(style["linewidth"], 4.5)
        self.assertEqual(style["color"], "#123456")

    def test_mass_series_values_can_normalize(self) -> None:
        values = [10.0, 8.0, 5.0]
        result = mass_series_values(values, normalized=True, smooth=False)
        self.assertEqual(result, [100.0, 80.0, 50.0])

    def test_dtg_returns_expected_shape(self) -> None:
        now = datetime.now()
        timestamps = [now, now + timedelta(minutes=1), now + timedelta(minutes=2)]
        values = [10.0, 7.0, 5.0]
        dtg = dtg_values(timestamps, values, smooth=False)
        self.assertEqual(len(dtg), 3)
        self.assertEqual(dtg[1], -3.0)
        self.assertEqual(dtg[2], -2.0)

    def test_append_series_sample_deduplicates_timestamp(self) -> None:
        timestamps = deque(maxlen=5)
        values = deque(maxlen=5)
        raw = datetime.now().isoformat()
        append_series_sample(timestamps, values, raw, 1.0)
        append_series_sample(timestamps, values, raw, 2.0)
        self.assertEqual(len(timestamps), 1)


if __name__ == "__main__":
    unittest.main()
