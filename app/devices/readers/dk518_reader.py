from __future__ import annotations

import logging
import time

from app.devices.runtime.simulated_values import (
    simulated_furnace_pv,
    simulated_furnace_sv,
)
from app.devices.transport.modbus_client import (
    open_modbus_serial_client,
    resolve_modbus_device_arg_name,
)
from app.models import FurnaceConfig


class DK518Reader:
    def __init__(
        self,
        config: FurnaceConfig,
        test_mode: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.test_mode = test_mode
        self.logger = logger or logging.getLogger(__name__)
        self._client = None
        self._next_connect_attempt_at = 0.0
        self._device_id_arg_name: str | None = None
        self._test_started = time.monotonic()

        if self.config.enabled and not self.test_mode:
            self.connect()

    def connect(self) -> bool:
        if not self.config.enabled or self.test_mode:
            return False

        self.close()

        try:
            self._client = open_modbus_serial_client(
                self.config,
                logger=self.logger,
                device_label="DK518",
            )
            if self._client is None:
                self._next_connect_attempt_at = time.monotonic() + 5.0
                return False
            self._next_connect_attempt_at = 0.0
            return True
        except Exception as exc:
            self.logger.warning("Не удалось инициализировать Modbus-клиент DK518 на %s: %s", self.config.port, exc)
            self._client = None
            self._next_connect_attempt_at = time.monotonic() + 5.0
            return False

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                self.logger.debug("Ignoring DK518 close error.", exc_info=True)
            finally:
                self._client = None

    @property
    def connected(self) -> bool:
        if self.test_mode:
            return True
        return self._client is not None

    def read_pv(self) -> float | None:
        if self.test_mode:
            elapsed = time.monotonic() - self._test_started
            return simulated_furnace_pv(elapsed)
        values = self._read_groups()
        return self._extract_role(values, role_key="pv_index")

    def read_sv(self) -> float | None:
        if self.test_mode:
            elapsed = time.monotonic() - self._test_started
            return simulated_furnace_sv(elapsed)
        values = self._read_groups()
        return self._extract_role(values, role_key="sv_index")

    def read_temperatures(self) -> tuple[float | None, float | None]:
        if self.test_mode:
            return self.read_pv(), self.read_sv()
        values = self._read_groups()
        return self._extract_role(values, role_key="pv_index"), self._extract_role(values, role_key="sv_index")

    def send_test_command(self, _name: str, _payload: object | None = None) -> bool:
        self.logger.warning("Режим тестовых команд для DK518 пока не реализован.")
        return False

    def _extract_role(self, groups: dict[str, list[float]] | None, *, role_key: str) -> float | None:
        if not groups:
            return None
        for group in self.config.read_groups:
            if role_key not in group:
                continue
            name = str(group.get("name", "")).strip()
            if not name:
                continue
            try:
                index = int(group.get(role_key))
            except (TypeError, ValueError):
                continue
            values = groups.get(name, [])
            if 0 <= index < len(values):
                return values[index]
        return None

    def _read_groups(self) -> dict[str, list[float]] | None:
        if not self.config.enabled:
            return None
        if not self._ensure_connection():
            return None

        results: dict[str, list[float]] = {}
        for group in self.config.read_groups:
            name = str(group.get("name", "")).strip() or "group"
            try:
                function_code = int(group.get("function", 3))
                address = int(group.get("address", 0))
                count = int(group.get("count", 1))
                scale = float(group.get("scale", 1.0))
            except (TypeError, ValueError):
                continue
            registers = self._read_registers(function_code, address, count)
            if registers is None:
                continue
            results[name] = [value * scale for value in registers]
        return results or None

    def _ensure_connection(self) -> bool:
        if self._client is not None:
            return True
        if time.monotonic() < self._next_connect_attempt_at:
            return False
        return self.connect()

    def _read_registers(self, function_code: int, address: int, count: int) -> list[int] | None:
        if self._client is None:
            return None

        device_arg = self._resolve_device_id_arg_name()
        kwargs = {
            "address": address,
            "count": count,
        }
        if device_arg is not None:
            kwargs[device_arg] = self.config.slave_id

        try:
            if function_code == 4:
                response = self._client.read_input_registers(**kwargs)
            else:
                response = self._client.read_holding_registers(**kwargs)
        except Exception:
            self.logger.warning(
                "Ошибка чтения Modbus регистра %s (func=%s) у устройства %s",
                address,
                function_code,
                self.config.slave_id,
                exc_info=True,
            )
            self.close()
            self._next_connect_attempt_at = time.monotonic() + 5.0
            return None

        if response is None:
            return None
        if hasattr(response, "isError") and response.isError():
            return None

        registers = getattr(response, "registers", None)
        if not registers:
            return None
        return [int(value) for value in registers]

    def _resolve_device_id_arg_name(self) -> str | None:
        if self._device_id_arg_name is not None:
            return self._device_id_arg_name

        if self._client is None:
            return None

        self._device_id_arg_name = resolve_modbus_device_arg_name(
            self._client,
            self._device_id_arg_name,
        )
        return self._device_id_arg_name
