from __future__ import annotations

import unittest

from app.devices.models.runtime_state import AcquisitionRuntimeState
from app.devices.models.status import ProbeResult
from app.devices.runtime.acquisition_support import (
    build_furnace_connection_status,
    build_furnace_status,
    build_scale_connection_status,
    build_scale_status,
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
            driver="dk518",
        ),
        app=ApplicationConfig(
            poll_interval_sec=1.0,
            csv_path="data/measurements.csv",
            log_path="logs/app.log",
            max_points_on_plot=500,
            test_mode=True,
            test_mode_scope="all",
        ),
    )


class DeviceStatusModelsTests(unittest.TestCase):
    def test_probe_result_tuple_compatibility(self) -> None:
        result = ProbeResult(True, "ok", "COM1", "scale")
        self.assertEqual(result.as_tuple(), (True, "ok"))

    def test_build_scale_status_uses_runtime_state(self) -> None:
        config = _make_config()
        state = AcquisitionRuntimeState(last_mass=12.5, last_mass_timestamp="ts")
        status = build_scale_status(config, state, connected=True)
        self.assertEqual(status.device_kind, "scale")
        self.assertEqual(status.value, 12.5)
        self.assertEqual(status.last_timestamp, "ts")

    def test_build_furnace_status_uses_both_values(self) -> None:
        config = _make_config()
        state = AcquisitionRuntimeState(
            last_furnace_pv=100.0,
            last_furnace_sv=200.0,
            last_furnace_pv_timestamp="pv-ts",
        )
        status = build_furnace_status(config, state, connected=True)
        self.assertEqual(status.value, 100.0)
        self.assertEqual(status.secondary_value, 200.0)
        self.assertEqual(status.last_timestamp, "pv-ts")

    def test_build_connection_statuses_preserve_ports(self) -> None:
        config = _make_config()
        scale = build_scale_connection_status(config, connected=True)
        furnace = build_furnace_connection_status(config, connected=False)
        self.assertEqual(scale.port, "COM1")
        self.assertEqual(furnace.port, "COM2")
        self.assertEqual(furnace.driver, "dk518")


if __name__ == "__main__":
    unittest.main()
