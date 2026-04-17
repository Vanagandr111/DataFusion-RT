from __future__ import annotations

import unittest

from app.devices.runtime.simulated_values import (
    simulated_furnace_pv,
    simulated_furnace_sv,
    simulated_scale_mass,
)
from app.devices.transport.modbus_client import resolve_modbus_device_arg_name


class _ClientWithDeviceId:
    def read_holding_registers(self, *, address, count, device_id):  # pragma: no cover - signature only
        return None


class _ClientWithSlave:
    def read_holding_registers(self, *, address, count, slave):  # pragma: no cover - signature only
        return None


class _ClientWithoutUnitArg:
    def read_holding_registers(self, *, address, count):  # pragma: no cover - signature only
        return None


class DeviceRuntimeHelpersTests(unittest.TestCase):
    def test_simulated_scale_mass_returns_float(self) -> None:
        value = simulated_scale_mass(10.0)
        self.assertIsInstance(value, float)

    def test_simulated_furnace_profile_is_consistent(self) -> None:
        self.assertLess(simulated_furnace_pv(0.0), simulated_furnace_pv(20.0))
        self.assertEqual(simulated_furnace_sv(0.0), 400.0)
        self.assertEqual(simulated_furnace_sv(35.0), 650.0)

    def test_resolve_modbus_device_arg_name_detects_device_id(self) -> None:
        self.assertEqual(
            resolve_modbus_device_arg_name(_ClientWithDeviceId(), None),
            "device_id",
        )

    def test_resolve_modbus_device_arg_name_detects_slave(self) -> None:
        self.assertEqual(
            resolve_modbus_device_arg_name(_ClientWithSlave(), None),
            "slave",
        )

    def test_resolve_modbus_device_arg_name_returns_none_for_unknown_signature(self) -> None:
        self.assertIsNone(resolve_modbus_device_arg_name(_ClientWithoutUnitArg(), None))


if __name__ == "__main__":
    unittest.main()
