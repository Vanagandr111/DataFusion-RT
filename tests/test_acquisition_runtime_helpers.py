from __future__ import annotations

import unittest

from app.devices.models.runtime_state import AcquisitionRuntimeState
from app.devices.runtime.acquisition_support import (
    furnace_poll_interval,
    scale_poll_interval,
)
from app.models import AppConfig, ApplicationConfig, FurnaceConfig, ScaleConfig


def _make_config() -> AppConfig:
    return AppConfig(
        scale=ScaleConfig(
            enabled=True,
            port="COM1",
            baudrate=9600,
            timeout=1.0,
            mode="auto",
            request_command="P\r\n",
            p1_polling_enabled=False,
            p1_poll_interval_sec=0.1,
        ),
        furnace=FurnaceConfig(
            enabled=True,
            port="COM2",
            baudrate=9600,
            bytesize=7,
            parity="E",
            stopbits=1.0,
            timeout=1.0,
            slave_id=1,
            register_pv=90,
            register_sv=91,
            scale_factor=0.1,
        ),
        app=ApplicationConfig(
            poll_interval_sec=1.0,
            csv_path="data/measurements.csv",
            log_path="logs/app.log",
            max_points_on_plot=500,
        ),
    )


class AcquisitionRuntimeHelpersTests(unittest.TestCase):
    def test_runtime_state_reset_clears_values(self) -> None:
        state = AcquisitionRuntimeState(
            sample_count=5,
            last_mass=1.23,
            last_furnace_pv=100.0,
            last_furnace_sv=200.0,
            last_mass_timestamp="x",
            last_furnace_pv_timestamp="y",
            last_furnace_sv_timestamp="z",
            next_scale_poll_at=10.0,
            next_furnace_poll_at=20.0,
        )
        state.reset()
        self.assertEqual(state.sample_count, 0)
        self.assertIsNone(state.last_mass)
        self.assertIsNone(state.last_furnace_pv)
        self.assertIsNone(state.last_furnace_sv)
        self.assertEqual(state.next_scale_poll_at, 0.0)
        self.assertEqual(state.next_furnace_poll_at, 0.0)

    def test_runtime_state_build_record_increments_counter(self) -> None:
        state = AcquisitionRuntimeState()
        state.record_mass(12.5)
        state.record_furnace(100.0, 200.0)
        record = state.build_record()
        self.assertEqual(state.sample_count, 1)
        self.assertEqual(record.mass, 12.5)
        self.assertEqual(record.furnace_pv, 100.0)
        self.assertEqual(record.furnace_sv, 200.0)

    def test_scale_poll_interval_uses_p1_when_enabled(self) -> None:
        config = _make_config()
        config.scale.p1_polling_enabled = True
        config.scale.p1_poll_interval_sec = 0.25
        self.assertEqual(scale_poll_interval(config), 0.25)

    def test_furnace_poll_interval_uses_app_poll_interval(self) -> None:
        config = _make_config()
        config.app.poll_interval_sec = 1.5
        self.assertEqual(furnace_poll_interval(config), 1.5)


if __name__ == "__main__":
    unittest.main()
