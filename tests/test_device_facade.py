from __future__ import annotations

import unittest

from app.devices.facade import DeviceFacade
from app.models import AppConfig, ApplicationConfig, FurnaceConfig, ScaleConfig


class _ScaleReaderStub:
    connected = True

    def read_mass(self) -> float | None:
        return 12.5


class _FurnaceReaderStub:
    connected = True

    def read_temperatures(self) -> tuple[float | None, float | None]:
        return 100.0, 200.0

    def read_pv(self) -> float | None:
        return 100.0

    def read_sv(self) -> float | None:
        return 200.0


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
            test_mode=False,
        ),
    )


class DeviceFacadeTests(unittest.TestCase):
    def test_sample_scale_updates_runtime_state(self) -> None:
        facade = DeviceFacade(_make_config())
        status = facade.sample_scale(_ScaleReaderStub())
        self.assertEqual(status.value, 12.5)
        self.assertEqual(facade.runtime_state.last_mass, 12.5)

    def test_sample_furnace_updates_runtime_state(self) -> None:
        facade = DeviceFacade(_make_config())
        status = facade.sample_furnace(_FurnaceReaderStub())
        self.assertEqual(status.value, 100.0)
        self.assertEqual(status.secondary_value, 200.0)
        self.assertEqual(facade.runtime_state.last_furnace_pv, 100.0)

    def test_connection_status_uses_reader_state(self) -> None:
        facade = DeviceFacade(_make_config())
        scale_status = facade.scale_connection_status(_ScaleReaderStub())
        furnace_status = facade.furnace_connection_status(_FurnaceReaderStub())
        self.assertTrue(scale_status.connected)
        self.assertTrue(furnace_status.connected)
        self.assertEqual(furnace_status.driver, "dk518")


if __name__ == "__main__":
    unittest.main()
