from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime

import serial

from app.devices.runtime.simulated_values import (
    simulated_furnace_pv,
    simulated_furnace_sv,
)
from app.devices.transport.serial_openers import open_passive_furnace_serial
from app.models import FurnaceConfig
from instruments.rs485_listener import ModbusFrame, ModbusRtuFrameParser, PendingRequest


MAX_PENDING_AGE_S = 10.0


@dataclass
class _ObservedValue:
    value: float
    observed_at: float


class PassiveFurnaceReader:
    def __init__(
        self,
        config: FurnaceConfig,
        test_mode: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.test_mode = test_mode
        self.logger = logger or logging.getLogger(__name__)
        self._serial: serial.Serial | None = None
        self._parser = ModbusRtuFrameParser(allow_masked_crc=self.config.bytesize == 7)
        self._pending_requests: deque[PendingRequest] = deque()
        self._next_connect_attempt_at = 0.0
        self._test_started = time.monotonic()
        self._pv: _ObservedValue | None = None
        self._sv: _ObservedValue | None = None
        self._connected_hold_s = max(3.0, float(self.config.timeout) * 8.0)

        if self.config.enabled and not self.test_mode:
            self.connect()

    def connect(self) -> bool:
        if not self.config.enabled or self.test_mode:
            return False

        self.close()
        try:
            self._serial = open_passive_furnace_serial(self.config)
            self._next_connect_attempt_at = 0.0
            self.logger.info("Пассивное прослушивание печи открыто на %s", self.config.port)
            return True
        except Exception as exc:
            self.logger.warning("Не удалось открыть пассивный порт печи %s: %s", self.config.port, exc)
            self._serial = None
            self._next_connect_attempt_at = time.monotonic() + 5.0
            return False

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                self.logger.debug("Ignoring passive furnace close error.", exc_info=True)
            finally:
                self._serial = None

    @property
    def connected(self) -> bool:
        if self.test_mode:
            return True
        now = time.monotonic()
        return bool(
            (self._pv and now - self._pv.observed_at <= self._connected_hold_s)
            or (self._sv and now - self._sv.observed_at <= self._connected_hold_s)
        )

    def read_pv(self) -> float | None:
        pv, _ = self.read_temperatures()
        return pv

    def read_sv(self) -> float | None:
        _, sv = self.read_temperatures()
        return sv

    def read_temperatures(self) -> tuple[float | None, float | None]:
        if self.test_mode:
            elapsed = time.monotonic() - self._test_started
            pv = simulated_furnace_pv(elapsed)
            sv = simulated_furnace_sv(elapsed)
            return pv, sv

        self._drain_serial()
        return self._pv.value if self._pv else None, self._sv.value if self._sv else None

    def _ensure_connection(self) -> bool:
        if self._serial is not None and self._serial.is_open:
            return True
        if time.monotonic() < self._next_connect_attempt_at:
            return False
        return self.connect()

    def _drain_serial(self) -> None:
        if not self.config.enabled:
            return
        if not self._ensure_connection() or self._serial is None:
            return

        try:
            waiting = self._serial.in_waiting
            if waiting <= 0:
                return
            chunk = self._serial.read(waiting)
            if not chunk:
                return
            frames = self._parser.feed(chunk, timestamp=datetime.now(), log_raw=False)
            for frame in frames:
                self._process_frame(frame)
        except Exception:
            self.logger.warning("Ошибка пассивного чтения печи.", exc_info=True)
            self.close()
            self._next_connect_attempt_at = time.monotonic() + 5.0

    def _process_frame(self, frame: ModbusFrame) -> None:
        self._purge_stale_requests(frame.timestamp)
        if frame.slave_id != self.config.slave_id:
            return
        if frame.is_read_request:
            self._queue_request(frame)
            return
        if not frame.is_read_response:
            return
        registers = frame.get_registers()
        if not registers:
            return
        request = self._pop_matching_request(frame, len(registers))
        if request is None:
            return
        self._apply_registers(request.address, registers)

    def _queue_request(self, frame: ModbusFrame) -> None:
        address = frame.starting_address
        quantity = frame.quantity
        if address is None or quantity is None:
            return
        end_address = address + quantity - 1
        if not (
            address <= self.config.register_pv <= end_address
            or address <= self.config.register_sv <= end_address
        ):
            return
        self._pending_requests.append(
            PendingRequest(
                timestamp=frame.timestamp,
                slave_id=frame.slave_id,
                function_code=frame.function_code,
                address=address,
                quantity=quantity,
                raw_bytes=frame.raw_bytes,
            )
        )

    def _purge_stale_requests(self, now: datetime) -> None:
        while self._pending_requests:
            age = (now - self._pending_requests[0].timestamp).total_seconds()
            if age <= MAX_PENDING_AGE_S:
                break
            self._pending_requests.popleft()

    def _pop_matching_request(self, frame: ModbusFrame, register_count: int) -> PendingRequest | None:
        matched: PendingRequest | None = None
        remainder: deque[PendingRequest] = deque()
        while self._pending_requests:
            request = self._pending_requests.popleft()
            if (
                matched is None
                and request.slave_id == frame.slave_id
                and request.function_code == frame.function_code
                and request.quantity == register_count
            ):
                matched = request
                continue
            remainder.append(request)
        self._pending_requests = remainder
        return matched

    def _apply_registers(self, base_address: int, registers: list[int]) -> None:
        now = time.monotonic()
        for index, raw in enumerate(registers):
            address = base_address + index
            value = float(raw) * float(self.config.scale_factor)
            if address == self.config.register_pv:
                self._pv = _ObservedValue(value=value, observed_at=now)
            elif address == self.config.register_sv:
                self._sv = _ObservedValue(value=value, observed_at=now)
