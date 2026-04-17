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


class FurnaceReader:
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
        self._test_started = time.monotonic()
        self._next_connect_attempt_at = 0.0
        self._device_id_arg_name: str | None = None

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
                device_label="печи",
            )
            if self._client is None:
                self._next_connect_attempt_at = time.monotonic() + 5.0
                return False
            self._next_connect_attempt_at = 0.0
            return True
        except Exception as exc:
            self.logger.warning("Не удалось инициализировать Modbus-клиент печи на %s: %s", self.config.port, exc)
            self._client = None
            self._next_connect_attempt_at = time.monotonic() + 5.0
            return False

    def read_pv(self) -> float | None:
        if self.test_mode:
            elapsed = time.monotonic() - self._test_started
            return simulated_furnace_pv(elapsed)
        return self._read_register_scaled(
            self.config.register_pv,
            label="PV",
            function_code=int(self.config.register_mode_pv),
        )

    def read_sv(self) -> float | None:
        if self.test_mode:
            elapsed = time.monotonic() - self._test_started
            return simulated_furnace_sv(elapsed)
        return self._read_register_scaled(
            self.config.register_sv,
            label="SV",
            function_code=int(self.config.register_mode_sv),
        )

    def read_temperatures(self) -> tuple[float | None, float | None]:
        if self.test_mode:
            return self.read_pv(), self.read_sv()
        if (
            self.config.register_sv == self.config.register_pv + 1
            and int(self.config.register_mode_pv) == int(self.config.register_mode_sv)
        ):
            registers = self._read_register_block(
                self.config.register_pv,
                2,
                function_code=int(self.config.register_mode_pv),
            )
            if registers and len(registers) >= 2:
                scale = self.config.scale_factor
                return float(registers[0]) * scale, float(registers[1]) * scale
        return self.read_pv(), self.read_sv()

    @property
    def connected(self) -> bool:
        if self.test_mode:
            return True
        return self._client is not None

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                self.logger.debug("Ignoring furnace close error.", exc_info=True)
            finally:
                self._client = None

    def _read_register_scaled(self, register: int, label: str, *, function_code: int) -> float | None:
        if not self.config.enabled:
            return None

        if not self._ensure_connection():
            return None

        try:
            response = self._read_register(register, function_code=function_code)
            if response is None:
                return None
            return float(response) * self.config.scale_factor
        except Exception:
            self.logger.exception(
                "Непредвиденная ошибка при чтении параметра печи %s из регистра %s",
                label,
                register,
            )
            self.close()
            self._next_connect_attempt_at = time.monotonic() + 5.0
            return None

    def _ensure_connection(self) -> bool:
        if self._client is not None:
            return True
        if time.monotonic() < self._next_connect_attempt_at:
            return False
        return self.connect()

    def _read_register(self, register: int, *, function_code: int) -> int | None:
        registers = self._read_register_block(register, 1, function_code=function_code)
        if not registers:
            return None
        return int(registers[0])

    def _read_register_block(self, register: int, count: int, *, function_code: int) -> list[int] | None:
        if self._client is None:
            return None

        try:
            response = self._read_registers(function_code, register, count)
        except Exception:
            self.logger.warning(
                "Ошибка чтения Modbus регистра %s у устройства %s",
                register,
                self.config.slave_id,
                exc_info=True,
            )
            self.close()
            self._next_connect_attempt_at = time.monotonic() + 5.0
            return None

        if response is None:
            self.logger.warning("Пустой ответ Modbus для регистра %s", register)
            return None

        if hasattr(response, "isError") and response.isError():
            self.logger.warning("Ошибка Modbus при чтении регистра %s: %s", register, response)
            return None

        registers = getattr(response, "registers", None)
        if not registers:
            self.logger.warning("Нет данных регистра в ответе для регистра %s", register)
            return None

        return [int(value) for value in registers]

    def _read_registers(self, function_code: int, register: int, count: int):
        if self._client is None:
            return None

        device_arg = self._resolve_device_id_arg_name()
        kwargs = {
            "address": register,
            "count": count,
        }
        if device_arg is not None:
            kwargs[device_arg] = self.config.slave_id

        if function_code == 4:
            return self._client.read_input_registers(**kwargs)
        return self._client.read_holding_registers(**kwargs)

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
