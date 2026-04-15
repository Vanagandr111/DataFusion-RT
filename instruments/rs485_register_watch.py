from __future__ import annotations

import argparse
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    import serial
except Exception as exc:  # pragma: no cover
    print(f"Не удалось импортировать pyserial: {exc}")
    print("Установите зависимости: python -m pip install -r requirements.txt")
    raise SystemExit(1)

from rs485_listener import (
    ConsoleLogger,
    ModbusFrame,
    ModbusRtuFrameParser,
    NullLogger,
    PendingRequest,
    SerialProfile,
    ask,
    ask_profile_name,
    make_log_path,
    normalize_port,
    resolve_profile,
)


MAX_PENDING_AGE_S = 10.0


@dataclass
class RegisterObservation:
    timestamp: datetime
    raw_value: int
    scaled_value: float
    display_value: int
    function_code: int
    address: int
    crc_status: str


class RegisterWatcher:
    def __init__(
        self,
        port: str,
        profile: SerialProfile,
        logger: ConsoleLogger,
        *,
        register_address: int,
        scale: float,
        duration_seconds: float,
        timeout: float,
        slave_id: int = 1,
        verbose: bool = False,
    ) -> None:
        self.port = port
        self.profile = profile
        self.logger = logger
        self.register_address = register_address
        self.scale = scale
        self.duration_seconds = duration_seconds
        self.timeout = timeout
        self.slave_id = slave_id
        self.verbose = verbose
        self.ser: serial.Serial | None = None
        self.pending_requests: deque[PendingRequest] = deque()
        self.parser = ModbusRtuFrameParser(
            logger if verbose else NullLogger(),
            allow_masked_crc=profile.bytesize == 7,
        )
        self.observations: list[RegisterObservation] = []
        self.last_raw_value: int | None = None
        self.last_display_value: int | None = None
        self.last_print_time = time.monotonic()

    def open(self) -> bool:
        parity_map = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}
        bytesize_map = {7: serial.SEVENBITS, 8: serial.EIGHTBITS}
        stopbits_map = {1.0: serial.STOPBITS_ONE, 1.5: serial.STOPBITS_ONE_POINT_FIVE, 2.0: serial.STOPBITS_TWO}
        try:
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.profile.baudrate,
                bytesize=bytesize_map[self.profile.bytesize],
                parity=parity_map[self.profile.parity],
                stopbits=stopbits_map[float(self.profile.stopbits)],
                timeout=self.timeout,
            )
            self.ser.rts = False
            self.ser.dtr = False
            self.logger.line(f"Порт {self.port} открыт: {self.profile.name} | {self.profile.wire_label}")
            return True
        except Exception as exc:
            self.logger.line(f"Ошибка открытия порта {self.port}: {exc}")
            return False

    def close(self) -> None:
        if self.ser and self.ser.is_open:
            self.ser.close()

    def _purge_stale_requests(self, now: datetime) -> None:
        while self.pending_requests:
            age = (now - self.pending_requests[0].timestamp).total_seconds()
            if age <= MAX_PENDING_AGE_S:
                break
            stale = self.pending_requests.popleft()
            if self.verbose:
                self.logger.line(
                    f"[MATCH {now.strftime('%H:%M:%S.%f')[:-3]}] "
                    f"Истёк запрос addr=0x{stale.address:04X} qty={stale.quantity} fc=0x{stale.function_code:02X}"
                )

    def _queue_request(self, frame: ModbusFrame) -> None:
        address = frame.starting_address
        quantity = frame.quantity
        if address is None or quantity is None:
            return
        end_address = address + quantity - 1
        if not (address <= self.register_address <= end_address):
            return
        self.pending_requests.append(
            PendingRequest(
                timestamp=frame.timestamp,
                slave_id=frame.slave_id,
                function_code=frame.function_code,
                address=address,
                quantity=quantity,
                raw_bytes=frame.raw_bytes,
            )
        )
        if self.verbose:
            self.logger.line(
                f"[MATCH {frame.timestamp.strftime('%H:%M:%S.%f')[:-3]}] "
                f"Запрос на интересующий диапазон: fc=0x{frame.function_code:02X} "
                f"addr=0x{address:04X}..0x{end_address:04X} queue={len(self.pending_requests)}"
            )

    def _match_response(self, frame: ModbusFrame, register_count: int) -> PendingRequest | None:
        matched: PendingRequest | None = None
        remainder: deque[PendingRequest] = deque()
        while self.pending_requests:
            request = self.pending_requests.popleft()
            if (
                matched is None
                and request.slave_id == frame.slave_id
                and request.function_code == frame.function_code
                and request.quantity == register_count
            ):
                matched = request
                continue
            remainder.append(request)
        self.pending_requests = remainder
        return matched

    def _record_observation(self, frame: ModbusFrame, request: PendingRequest, registers: list[int]) -> None:
        offset = self.register_address - request.address
        if offset < 0 or offset >= len(registers):
            return
        raw_value = registers[offset]
        scaled_value = raw_value * self.scale
        display_value = round(scaled_value)
        observation = RegisterObservation(
            timestamp=frame.timestamp,
            raw_value=raw_value,
            scaled_value=scaled_value,
            display_value=display_value,
            function_code=frame.function_code,
            address=self.register_address,
            crc_status=frame.crc_status,
        )
        self.observations.append(observation)

        changed = self.last_raw_value is None or self.last_raw_value != raw_value
        display_changed = self.last_display_value is None or self.last_display_value != display_value
        if changed or self.verbose:
            delta_text = ""
            if self.last_raw_value is not None:
                delta_text = f" | Δraw={raw_value - self.last_raw_value:+d}"
            screen_mark = " <- экран изменился" if display_changed else ""
            self.logger.line(
                f"[{frame.timestamp.strftime('%H:%M:%S')}] "
                f"Предполагаемая температура: {display_value:>3} C "
                f"(точнее {scaled_value:>5.2f} C, raw={raw_value}){delta_text}{screen_mark}"
            )
        self.last_display_value = display_value
        self.last_raw_value = raw_value

    def _fc_label(self, function_code: int) -> str:
        return "INPUT" if function_code == 4 else "HOLD" if function_code == 3 else f"FC{function_code:02X}"

    def process_frame(self, frame: ModbusFrame) -> None:
        self._purge_stale_requests(frame.timestamp)
        if frame.slave_id != self.slave_id:
            return
        if frame.is_read_request:
            self._queue_request(frame)
            return
        if not frame.is_read_response:
            return
        registers = frame.get_registers()
        if not registers:
            return
        request = self._match_response(frame, len(registers))
        if request is None:
            return
        self._record_observation(frame, request, registers)

    def _print_summary(self) -> None:
        self.logger.section("Итог")
        self.logger.line(f"Регистр: 0x{self.register_address:04X} ({self.register_address})")
        self.logger.line(f"Scale: x{self.scale:g}")
        self.logger.line(f"Наблюдений: {len(self.observations)}")
        if not self.observations:
            self.logger.line("Совпадений по регистру не найдено.")
            return
        raw_values = [item.raw_value for item in self.observations]
        scaled_values = [item.scaled_value for item in self.observations]
        displays = [item.display_value for item in self.observations]
        self.logger.line(
            f"raw min/avg/max: {min(raw_values)} / {sum(raw_values) / len(raw_values):.2f} / {max(raw_values)}"
        )
        self.logger.line(
            f"scaled min/avg/max: {min(scaled_values):.2f} / "
            f"{sum(scaled_values) / len(scaled_values):.2f} / {max(scaled_values):.2f}"
        )
        self.logger.line(f"screen-rounded unique: {sorted(set(displays))}")
        self.logger.line(f"Предполагаемое текущее значение: {displays[-1]} C ({scaled_values[-1]:.2f} C)")

    def run(self) -> None:
        if not self.ser or not self.ser.is_open:
            self.logger.line("Порт не открыт.")
            return

        self.logger.section("RS-485 register watch")
        self.logger.line(f"Порт: {self.port}")
        self.logger.line(f"Профиль: {self.profile.name} | {self.profile.wire_label}")
        self.logger.line(f"Slave: {self.slave_id}")
        self.logger.line(f"Регистр: 0x{self.register_address:04X} ({self.register_address})")
        self.logger.line(f"Scale: x{self.scale:g}")
        self.logger.line(f"Длительность: {self.duration_seconds:.1f} c")
        self.logger.line("Режим: пассивное отслеживание предполагаемой температуры.")
        self.logger.line("Показываются только изменения значения.")
        if self.verbose:
            self.logger.line("Debug-режим включён: transport и matching логи будут печататься подробно.")

        deadline = time.monotonic() + self.duration_seconds
        try:
            while time.monotonic() < deadline:
                waiting = self.ser.in_waiting
                if waiting:
                    chunk = self.ser.read(waiting)
                    if chunk:
                        frames = self.parser.feed(chunk, timestamp=datetime.now(), log_raw=self.verbose)
                        for frame in frames:
                            self.process_frame(frame)
                else:
                    time.sleep(0.001)

                now = time.monotonic()
                if self.verbose and now - self.last_print_time > 5.0:
                    self.logger.line(
                        f"[Статистика] Наблюдений: {len(self.observations)}, pending: {len(self.pending_requests)}"
                    )
                    self.last_print_time = now
        except KeyboardInterrupt:
            self.logger.line("")
            self.logger.line("Остановлено пользователем.")
        finally:
            self._print_summary()


def parse_register_address(raw: str) -> int:
    text = raw.strip()
    if text.lower().startswith("0x"):
        return int(text, 16)
    return int(text, 10)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Пассивное отслеживание одного Modbus-регистра в RS-485 трафике.")
    parser.add_argument("--port", help="COM порт, например COM11")
    parser.add_argument("--profile", choices=["dk518_7e1", "alt_8n1", "custom"], help="Готовый serial-профиль")
    parser.add_argument("--baud", type=int, default=9600, help="Скорость")
    parser.add_argument("--bits", type=int, default=7, choices=[7, 8], help="Биты данных")
    parser.add_argument("--parity", default="E", choices=["N", "E", "O"], help="Чётность")
    parser.add_argument("--stopbits", type=float, default=1.0, help="Стоп-биты")
    parser.add_argument("--timeout", type=float, default=0.1, help="Serial timeout")
    parser.add_argument("--duration", type=float, default=90.0, help="Длительность прослушивания в секундах")
    parser.add_argument("--address", default="0x005A", help="Адрес регистра, например 0x005A или 90")
    parser.add_argument("--scale", type=float, default=0.1, help="Коэффициент перевода raw -> экран")
    parser.add_argument("--slave-id", type=int, default=1, help="Modbus slave id")
    parser.add_argument("--log", help="Явный путь к лог-файлу")
    parser.add_argument("--verbose", action="store_true", help="Печатать подробные debug-логи")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    raw_port = args.port or ask("Введите COM-порт", "COM11")
    port = normalize_port(raw_port)

    profile_name = args.profile
    if not profile_name:
        profile_name = ask_profile_name()
    profile = resolve_profile(
        None if profile_name == "custom" else profile_name,
        args.baud,
        args.bits,
        args.parity,
        args.stopbits,
    )

    raw_address = args.address
    if not raw_address:
        raw_address = ask("Адрес регистра", "0x005A")
    register_address = parse_register_address(raw_address)

    log_path = Path(args.log) if args.log else make_log_path("rs485_watch")
    logger = ConsoleLogger(log_path)
    logger.line(f"Лог: {log_path}")
    logger.line(f"Порт: {port}")
    logger.line(f"Выбранный профиль: {profile.name}")
    logger.line(f"Параметры линии: {profile.wire_label}")
    logger.line(f"Регистр: 0x{register_address:04X} ({register_address})")
    logger.line(f"Scale: x{args.scale:g}")
    logger.line("Фокус: предполагаемая температура по выбранному регистру.")
    try:
        watcher = RegisterWatcher(
            port=port,
            profile=profile,
            logger=logger,
            register_address=register_address,
            scale=args.scale,
            duration_seconds=args.duration,
            timeout=args.timeout,
            slave_id=args.slave_id,
            verbose=args.verbose,
        )
        if not watcher.open():
            return 1
        try:
            watcher.run()
        finally:
            watcher.close()
        logger.line("")
        logger.line(f"Файл лога: {log_path}")
        return 0
    finally:
        logger.close()


if __name__ == "__main__":
    raise SystemExit(main())
