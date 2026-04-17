from __future__ import annotations

import logging
import threading
import time

import serial

from app.devices.runtime.simulated_values import simulated_scale_mass
from app.devices.transport.serial_openers import open_scale_serial
from app.models import ScaleConfig
from app.utils.parsers import parse_mass_line, sanitize_ascii_line


class ScaleReader:
    def __init__(
        self,
        config: ScaleConfig,
        test_mode: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.test_mode = test_mode
        self.logger = logger or logging.getLogger(__name__)
        self._serial: serial.Serial | None = None
        self._test_started = time.monotonic()
        self._next_connect_attempt_at = 0.0
        self._unparsed_line_count = 0
        self._last_polled_mass: float | None = None
        self._last_poll_at = 0.0
        self._last_valid_mass_at = 0.0
        self._connected_hold_s = max(3.0, float(self.config.timeout) * 8.0)
        self._io_lock = threading.RLock()
        self._mass_buffer: list[float] = []
        self._buffer_size: int = 5

        if self.config.enabled and not self.test_mode:
            self.connect()

    def connect(self) -> bool:
        if not self.config.enabled or self.test_mode:
            return False

        self.close()

        try:
            self._serial = open_scale_serial(self.config)
            self._next_connect_attempt_at = 0.0
            self._unparsed_line_count = 0
            self.logger.info("Подключение к весам открыто на %s", self.config.port)
            return True
        except serial.SerialException as exc:
            self.logger.warning(
                "Не удалось открыть порт весов %s: %s", self.config.port, exc
            )
            self._serial = None
            self._next_connect_attempt_at = time.monotonic() + 5.0
            return False

    def read_mass(self) -> float | None:
        if not self.config.enabled:
            return None

        if self.test_mode:
            elapsed = time.monotonic() - self._test_started
            raw_mass = simulated_scale_mass(elapsed)
            return self._apply_median_filter(raw_mass)

        if not self._ensure_connection():
            return None

        mode = self.config.mode.lower()

        try:
            with self._io_lock:
                if self.config.p1_polling_enabled:
                    raw_mass = self._poll_mass_rate_limited()
                elif mode == "continuous":
                    raw_mass = self._read_from_stream()
                elif mode == "poll":
                    raw_mass = self._poll_mass()
                else:
                    raw_mass = self._read_from_stream()
                    if raw_mass is None:
                        raw_mass = self._poll_mass()
                return self._apply_median_filter(raw_mass)
        except serial.SerialException:
            self.logger.warning("Ошибка обмена данными с весами.")
            self.close()
            self._next_connect_attempt_at = time.monotonic() + 5.0
            return None
        except OSError:
            self.logger.warning("Порт весов вернул системную ошибку.")
            self.close()
            self._next_connect_attempt_at = time.monotonic() + 5.0
            return None

    @property
    def connected(self) -> bool:
        if self.test_mode:
            return True
        return (
            self._serial is not None
            and self._serial.is_open
            and (time.monotonic() - self._last_valid_mass_at) <= self._connected_hold_s
        )

    def tare(self) -> bool:
        return self._send_verified_zeroing("T\r\n", action_name="тары")

    def zero(self) -> bool:
        return self._send_verified_zeroing("Z\r\n", action_name="обнуления")

    def send_command(self, command: str) -> bool:
        if not self.config.enabled:
            return False

        if self.test_mode:
            self.logger.info("Тестовый режим весов: команда имитирована %r", command)
            return True

        if not self._ensure_connection() or self._serial is None:
            self.logger.warning("Команда весам пропущена: порт не подключён.")
            return False

        try:
            with self._io_lock:
                self._serial.write(command.encode("ascii"))
                self._serial.flush()
            self.logger.info("Команда весам отправлена: %r", command)
            return True
        except serial.SerialException:
            self.logger.warning("Не удалось отправить команду весам %r", command)
            self.close()
            self._next_connect_attempt_at = time.monotonic() + 5.0
            return False

    def _send_verified_zeroing(self, command: str, *, action_name: str) -> bool:
        if not self.config.enabled:
            return False

        last_mass: float | None = None
        for attempt in range(1, 3):
            if not self.send_command(command):
                return False
            if self.test_mode:
                return True
            time.sleep(min(0.45, max(0.18, self.config.timeout)))
            last_mass = self.read_mass()
            if last_mass is not None and abs(last_mass) <= 0.005:
                self.logger.info(
                    "Команда %s подтверждена после попытки %s.", action_name, attempt
                )
                return True
            if attempt == 1:
                self.logger.warning(
                    "После команды %s масса ещё не нулевая (%s). Повторяем один раз.",
                    action_name,
                    last_mass,
                )

        self.logger.warning(
            "Команда %s отправлена дважды, но масса осталась ненулевой: %s",
            action_name,
            last_mass,
        )
        return False

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                self.logger.debug(
                    "Ошибка закрытия порта весов проигнорирована.", exc_info=True
                )
            finally:
                self._serial = None

    def _apply_median_filter(self, mass: float | None) -> float | None:
        if mass is None:
            return None
        with self._io_lock:
            self._mass_buffer.append(mass)
            if len(self._mass_buffer) > self._buffer_size:
                self._mass_buffer.pop(0)
            if not self._mass_buffer:
                return None
            sorted_buffer = sorted(self._mass_buffer)
            median = sorted_buffer[len(sorted_buffer) // 2]
            return median

    def _ensure_connection(self) -> bool:
        if self._serial is not None and self._serial.is_open:
            return True
        if time.monotonic() < self._next_connect_attempt_at:
            return False
        return self.connect()

    def _read_from_stream(self) -> float | None:
        if self._serial is None:
            return None

        latest_mass: float | None = None
        max_reads = 5 if self._serial.in_waiting else 1
        read_timeout = (
            0.05 if self._serial.in_waiting else min(0.2, self.config.timeout)
        )

        for _ in range(max_reads):
            raw_line = self._read_line(timeout_override=read_timeout)
            if not raw_line:
                continue
            mass = parse_mass_line(raw_line, self.logger)
            if mass is not None:
                self._unparsed_line_count = 0
                self._last_valid_mass_at = time.monotonic()
                latest_mass = mass
            else:
                self._log_unparsed_line(raw_line)

        return latest_mass

    def _poll_mass(self) -> float | None:
        if self._serial is None:
            return None

        try:
            self._serial.reset_input_buffer()
        except serial.SerialException:
            self.logger.debug("Не удалось очистить входной буфер весов.", exc_info=True)

        self._serial.write(self.config.request_command.encode("ascii"))
        self._serial.flush()

        response_timeout = max(0.25, self.config.timeout)
        for _ in range(3):
            raw_line = self._read_line(timeout_override=response_timeout)
            if not raw_line:
                continue
            mass = parse_mass_line(raw_line, self.logger)
            if mass is not None:
                self._unparsed_line_count = 0
                self._last_polled_mass = mass
                self._last_poll_at = time.monotonic()
                self._last_valid_mass_at = self._last_poll_at
                return mass
            self._log_unparsed_line(raw_line)

        self.logger.warning(
            "Весы не вернули распознаваемый ответ после команды %r",
            self.config.request_command,
        )
        return None

    def _poll_mass_rate_limited(self) -> float | None:
        now = time.monotonic()
        interval = max(0.02, float(self.config.p1_poll_interval_sec))
        if self._last_poll_at and (now - self._last_poll_at) < interval:
            return self._last_polled_mass
        return self._poll_mass()

    def _read_line(self, timeout_override: float | None = None) -> str | None:
        if self._serial is None:
            return None

        original_timeout = self._serial.timeout
        if timeout_override is not None:
            self._serial.timeout = timeout_override

        try:
            raw = self._serial.read_until(expected=b"\n", size=128)
        finally:
            if timeout_override is not None:
                self._serial.timeout = original_timeout

        if not raw:
            return None

        text = raw.decode("ascii", errors="ignore").strip()
        if text:
            self.logger.debug("Сырые данные весов: %r", text)
        return text or None

    def _log_unparsed_line(self, raw_line: str) -> None:
        self._unparsed_line_count += 1
        cleaned = sanitize_ascii_line(raw_line).upper()
        status_like = (
            cleaned in {"+", "-", "G", "KG", "MG", "OZ", "LB", "LBS"}
            or cleaned.endswith(" LLLLLL G")
            or cleaned == "LLLLLL G"
            or "LLLLLL" in cleaned
        )
        if status_like:
            message = "Статусная строка весов #%s: %r"
            if self._unparsed_line_count <= 5 or self._unparsed_line_count % 20 == 0:
                self.logger.info(message, self._unparsed_line_count, raw_line)
            else:
                self.logger.debug(message, self._unparsed_line_count, raw_line)
        else:
            message = "Не удалось распознать строку веса #%s: %r"
            if self._unparsed_line_count <= 5 or self._unparsed_line_count % 20 == 0:
                self.logger.warning(message, self._unparsed_line_count, raw_line)
            else:
                self.logger.debug(message, self._unparsed_line_count, raw_line)
