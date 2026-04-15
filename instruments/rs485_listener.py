from __future__ import annotations

import argparse
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import serial
    from serial import Serial
except Exception as exc:  # pragma: no cover
    print(f"Не удалось импортировать pyserial: {exc}")
    print("Установите зависимости: python -m pip install -r requirements.txt")
    raise SystemExit(1)

try:
    import yaml
except Exception as exc:  # pragma: no cover
    print(f"Не удалось импортировать PyYAML: {exc}")
    print("Установите зависимости: python -m pip install -r requirements.txt")
    raise SystemExit(1)


READ_FUNCTIONS = {3, 4}
EXCEPTION_FUNCTIONS = {function | 0x80 for function in READ_FUNCTIONS}
LOG_DIR = Path("logs")
MAX_PENDING_AGE_S = 10.0
MAX_HISTORY_PER_REGISTER = 200
MAX_RTU_BUFFER = 4096
TEMPERATURE_SCALES = (1.0, 0.1, 0.01)
PROFILE_PRESETS: dict[str, tuple[int, int, str, float]] = {
    "dk518_7e1": (9600, 7, "E", 1.0),
    "alt_8n1": (9600, 8, "N", 1.0),
}


@dataclass
class FurnaceConfig:
    register_pv: int = 0
    scale_factor: float = 0.1
    slave_id: int = 1
    known_addresses: list[int] | None = None


@dataclass(frozen=True)
class SerialProfile:
    name: str
    baudrate: int
    bytesize: int
    parity: str
    stopbits: float

    @property
    def wire_label(self) -> str:
        stop = int(self.stopbits) if float(self.stopbits).is_integer() else self.stopbits
        return f"{self.baudrate} {self.bytesize}{self.parity}{stop}"


@dataclass(frozen=True)
class ModbusFrame:
    timestamp: datetime
    raw_bytes: bytes
    decoded_bytes: bytes
    slave_id: int
    function_code: int
    payload: bytes
    frame_kind: str
    crc_status: str

    @property
    def is_read_request(self) -> bool:
        return self.frame_kind == "request" and self.function_code in READ_FUNCTIONS

    @property
    def is_read_response(self) -> bool:
        return self.frame_kind == "response" and self.function_code in READ_FUNCTIONS

    @property
    def is_exception(self) -> bool:
        return self.frame_kind == "exception"

    @property
    def byte_count(self) -> Optional[int]:
        if self.is_read_response and self.payload:
            return self.payload[0]
        return None

    @property
    def starting_address(self) -> Optional[int]:
        if self.is_read_request and len(self.payload) >= 4:
            return (self.payload[0] << 8) | self.payload[1]
        return None

    @property
    def quantity(self) -> Optional[int]:
        if self.is_read_request and len(self.payload) >= 4:
            return (self.payload[2] << 8) | self.payload[3]
        return None

    def get_registers(self) -> list[int]:
        if not self.is_read_response:
            return []
        byte_count = self.byte_count
        if byte_count is None or len(self.payload) < byte_count + 1:
            return []
        data_bytes = self.payload[1 : 1 + byte_count]
        registers: list[int] = []
        for index in range(0, len(data_bytes), 2):
            if index + 1 >= len(data_bytes):
                break
            registers.append((data_bytes[index] << 8) | data_bytes[index + 1])
        return registers


@dataclass
class PendingRequest:
    timestamp: datetime
    slave_id: int
    function_code: int
    address: int
    quantity: int
    raw_bytes: bytes


@dataclass
class RegisterSnapshot:
    function_code: int
    address: int
    values: list[int]
    timestamp: datetime


@dataclass
class RegisterCandidate:
    label: str
    function_code: int
    address: int
    samples: int
    avg_raw: float
    min_raw: int
    max_raw: int
    last_raw: int
    target_name: str
    target_value: float
    best_scale: float
    best_value: float
    abs_error: float


class ConsoleLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8-sig")

    def close(self) -> None:
        self._fh.close()

    def line(self, text: str = "") -> None:
        print(text)
        self._fh.write(text + "\n")
        self._fh.flush()

    def section(self, title: str) -> None:
        bar = "=" * len(title)
        self.line("")
        self.line(title)
        self.line(bar)


class NullLogger:
    def line(self, text: str = "") -> None:
        _ = text

    def section(self, title: str) -> None:
        _ = title


def ensure_logs_dir() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR


def make_log_path(prefix: str) -> Path:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return ensure_logs_dir() / f"{prefix}_{ts}.txt"


def normalize_port(raw: str) -> str:
    value = raw.strip()
    if not value:
        return value
    if value.isdigit():
        return f"COM{value}"
    if value.lower().startswith("com"):
        return value.upper()
    return value


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def resolve_profile(profile_name: str | None, baud: int, bits: int, parity: str, stopbits: float) -> SerialProfile:
    if profile_name:
        preset = PROFILE_PRESETS[profile_name]
        return SerialProfile(profile_name, preset[0], preset[1], preset[2], preset[3])
    return SerialProfile("custom", baud, bits, parity, stopbits)


def ask_profile_name() -> str:
    print("")
    print("Выберите serial-профиль:")
    print("1 - dk518_7e1 (9600 7E1, основной)")
    print("2 - alt_8n1   (9600 8N1, для сравнения)")
    print("3 - custom    (ввести вручную через аргументы)")
    choice = ask("Профиль", "1")
    if choice == "2":
        return "alt_8n1"
    if choice == "3":
        return "custom"
    return "dk518_7e1"


def ask_optional_float(prompt: str) -> float | None:
    value = input(f"{prompt} [Enter=пропустить]: ").strip().replace(",", ".")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        print("Неверное число, поле пропущено.")
        return None


def load_furnace_config(config_path: Path) -> FurnaceConfig:
    if not config_path.exists():
        return FurnaceConfig()
    try:
        with config_path.open("r", encoding="utf-8") as fh:
            root = yaml.safe_load(fh) or {}
    except Exception:
        return FurnaceConfig()

    furnace = root.get("furnace") or {}
    read_groups = furnace.get("read_groups") or []
    known_addresses: list[int] = []
    for group in read_groups:
        if isinstance(group, dict) and group.get("address") is not None:
            known_addresses.append(int(group["address"]))

    return FurnaceConfig(
        register_pv=int(furnace.get("register_pv", 0) or 0),
        scale_factor=float(furnace.get("scale_factor", 0.1) or 0.1),
        slave_id=int(furnace.get("slave_id", 1) or 1),
        known_addresses=known_addresses,
    )


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc >>= 1
                crc ^= 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def is_valid_modbus_crc(frame_bytes: bytes) -> bool:
    if len(frame_bytes) < 4:
        return False
    body = frame_bytes[:-2]
    received_crc = int.from_bytes(frame_bytes[-2:], "little")
    return crc16_modbus(body) == received_crc


def build_modbus_frame(raw_bytes: bytes, frame_kind: str, timestamp: datetime) -> ModbusFrame:
    return build_modbus_frame_with_decoded(raw_bytes, raw_bytes, frame_kind, "EXACT", timestamp)


def build_modbus_frame_with_decoded(
    raw_bytes: bytes,
    decoded_bytes: bytes,
    frame_kind: str,
    crc_status: str,
    timestamp: datetime,
) -> ModbusFrame:
    body = decoded_bytes[:-2]
    return ModbusFrame(
        timestamp=timestamp,
        raw_bytes=raw_bytes,
        decoded_bytes=decoded_bytes,
        slave_id=body[0],
        function_code=body[1],
        payload=body[2:],
        frame_kind=frame_kind,
        crc_status=crc_status,
    )


def validate_rtu_candidate(frame_bytes: bytes, *, allow_masked_crc: bool) -> tuple[bool, str, bytes | None]:
    if len(frame_bytes) < 4:
        return (False, "FAIL", None)
    if is_valid_modbus_crc(frame_bytes):
        return (True, "EXACT", frame_bytes)
    if not allow_masked_crc:
        return (False, "FAIL", None)

    body = frame_bytes[:-2]
    received_lo = frame_bytes[-2]
    received_hi = frame_bytes[-1]
    if len(body) > 16:
        return (False, "FAIL", None)

    for mask in range(1 << len(body)):
        candidate_body = bytearray(body)
        for index in range(len(body)):
            if mask & (1 << index):
                candidate_body[index] |= 0x80
        crc = crc16_modbus(bytes(candidate_body))
        if (crc & 0x7F) == received_lo and ((crc >> 8) & 0x7F) == received_hi:
            decoded = bytes(candidate_body) + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
            return (True, "MASKED", decoded)
    return (False, "FAIL", None)


class ModbusRtuFrameParser:
    def __init__(
        self,
        logger: ConsoleLogger | NullLogger | None = None,
        *,
        allow_masked_crc: bool = True,
    ) -> None:
        self.logger = logger or NullLogger()
        self.buffer = bytearray()
        self.allow_masked_crc = allow_masked_crc

    def feed(self, chunk: bytes, *, timestamp: datetime | None = None, log_raw: bool = True) -> list[ModbusFrame]:
        if not chunk:
            return []
        ts = timestamp or datetime.now()
        if log_raw:
            self.logger.line(f"[RAW {ts.strftime('%H:%M:%S.%f')[:-3]}] {chunk.hex(' ').upper()}")
        self.buffer.extend(chunk)
        if len(self.buffer) > MAX_RTU_BUFFER:
            overflow = len(self.buffer) - MAX_RTU_BUFFER
            dropped = bytes(self.buffer[:overflow])
            del self.buffer[:overflow]
            self.logger.line(
                f"[SYNC {ts.strftime('%H:%M:%S.%f')[:-3]}] Буфер переполнен, отброшено {len(dropped)} байт: "
                f"{dropped.hex(' ').upper()}"
            )
        return self._extract_frames(ts)

    def drain_remainder(self) -> bytes:
        remainder = bytes(self.buffer)
        self.buffer.clear()
        return remainder

    def _extract_frames(self, timestamp: datetime) -> list[ModbusFrame]:
        frames: list[ModbusFrame] = []
        while self.buffer:
            action, frame_kind, frame_len, reason, decoded_bytes, crc_status = self._probe_next_frame()
            time_str = timestamp.strftime("%H:%M:%S.%f")[:-3]
            if action == "need_more":
                break
            if action == "drop":
                dropped = self.buffer[0]
                del self.buffer[0]
                self.logger.line(f"[SYNC {time_str}] Отброшен байт 0x{dropped:02X}: {reason}")
                continue

            frame_bytes = bytes(self.buffer[:frame_len])
            del self.buffer[:frame_len]
            frame = build_modbus_frame_with_decoded(
                frame_bytes,
                decoded_bytes or frame_bytes,
                frame_kind,
                crc_status,
                timestamp,
            )
            frames.append(frame)
            self.logger.line(
                f"[FRAME {time_str}] {frame_kind.upper():<8} slave=0x{frame.slave_id:02X} "
                f"fc=0x{frame.function_code:02X} len={len(frame_bytes)} crc={frame.crc_status} | "
                f"raw={frame_bytes.hex(' ').upper()}"
            )
            if frame.crc_status == "MASKED":
                self.logger.line(f"[CRC  {time_str}] decoded={frame.decoded_bytes.hex(' ').upper()}")
        return frames

    def _probe_next_frame(self) -> tuple[str, str, int, str, bytes | None, str]:
        if len(self.buffer) < 2:
            return ("need_more", "", 0, "нужно минимум 2 байта", None, "FAIL")

        slave_id = self.buffer[0]
        function_code = self.buffer[1]

        if slave_id == 0 or slave_id > 247:
            return ("drop", "", 0, f"некорректный slave=0x{slave_id:02X}", None, "FAIL")

        if function_code in READ_FUNCTIONS:
            return self._probe_read_frame()
        if function_code in EXCEPTION_FUNCTIONS:
            return self._probe_exception_frame()
        return ("drop", "", 0, f"неподдерживаемый function=0x{function_code:02X}", None, "FAIL")

    def _probe_exception_frame(self) -> tuple[str, str, int, str, bytes | None, str]:
        frame_len = 5
        if len(self.buffer) < frame_len:
            return ("need_more", "", 0, "неполный exception frame", None, "FAIL")
        candidate = bytes(self.buffer[:frame_len])
        valid, crc_status, decoded = validate_rtu_candidate(candidate, allow_masked_crc=self.allow_masked_crc)
        if valid:
            return ("frame", "exception", frame_len, "crc ok", decoded, crc_status)
        return ("drop", "", 0, "CRC не сошёлся для exception frame", None, "FAIL")

    def _probe_read_frame(self) -> tuple[str, str, int, str, bytes | None, str]:
        candidates: list[tuple[str, int]] = []
        needs_more = False

        if len(self.buffer) >= 3:
            byte_count = self.buffer[2]
            if 0 < byte_count <= 250 and byte_count % 2 == 0:
                response_len = 5 + byte_count
                if len(self.buffer) >= response_len:
                    candidates.append(("response", response_len))
                else:
                    needs_more = True
        else:
            needs_more = True

        request_len = 8
        if len(self.buffer) >= request_len:
            quantity = (self.buffer[4] << 8) | self.buffer[5] if len(self.buffer) >= 6 else 0
            if 0 < quantity <= 125:
                candidates.append(("request", request_len))
        else:
            needs_more = True

        for frame_kind, frame_len in candidates:
            candidate = bytes(self.buffer[:frame_len])
            valid, crc_status, decoded = validate_rtu_candidate(candidate, allow_masked_crc=self.allow_masked_crc)
            if valid:
                return ("frame", frame_kind, frame_len, "crc ok", decoded, crc_status)

        if needs_more:
            return ("need_more", "", 0, "ожидаем завершения кадра", None, "FAIL")
        return ("drop", "", 0, "не найден валидный request/response CRC", None, "FAIL")


def split_frames_from_bytes(data: bytes) -> list[bytes]:
    parser = ModbusRtuFrameParser()
    return [frame.raw_bytes for frame in parser.feed(data, log_raw=False)]


def split_frames_from_hex(hex_string: str) -> list[bytes]:
    clean = "".join(hex_string.split())
    return split_frames_from_bytes(bytes.fromhex(clean))


def run_parser_self_test() -> tuple[bool, list[str]]:
    cases = [
        (
            "response_then_request_04_03",
            "01 04 06 02 31 01 74 00 60 5D 4D 01 03 00 15 00 03 14 0F",
            [
                "01 04 06 02 31 01 74 00 60 5D 4D",
                "01 03 00 15 00 03 14 0F",
            ],
        ),
        (
            "response_then_request_03_03",
            "01 03 06 00 00 00 01 00 02 71 74 01 03 00 56 00 03 65 5B",
            [
                "01 03 06 00 00 00 01 00 02 71 74",
                "01 03 00 56 00 03 65 5B",
            ],
        ),
    ]

    messages: list[str] = []
    ok = True
    for case_name, sample_hex, expected_frames in cases:
        actual_frames = [frame.hex(" ").upper() for frame in split_frames_from_hex(sample_hex)]
        if actual_frames != expected_frames:
            ok = False
            messages.append(
                f"{case_name}: FAIL | ожидалось={expected_frames} | получено={actual_frames}"
            )
        else:
            messages.append(f"{case_name}: OK | кадров={len(actual_frames)}")
    return ok, messages


class RS485Listener:
    def __init__(
        self,
        port: str,
        profile: SerialProfile,
        timeout: float,
        furnace: FurnaceConfig,
        logger: ConsoleLogger | NullLogger,
        *,
        chamber_temp: float | None,
        setpoint_temp: float | None,
    ) -> None:
        self.port = port
        self.profile = profile
        self.timeout = timeout
        self.furnace = furnace
        self.logger = logger
        self.ser: Optional[Serial] = None
        self.valid_frames = 0
        self.unmatched_responses = 0
        self.stale_requests = 0
        self.last_print_time = time.monotonic()
        self.pending_requests: deque[PendingRequest] = deque()
        self.snapshots: list[RegisterSnapshot] = []
        self.history: dict[tuple[int, int], list[int]] = {}
        self.chamber_temp = chamber_temp
        self.setpoint_temp = setpoint_temp
        self.parser = ModbusRtuFrameParser(logger, allow_masked_crc=profile.bytesize == 7)

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

    def _frame_type_label(self, function_code: int) -> str:
        if function_code == 3:
            return "HOLD"
        if function_code == 4:
            return "INPUT"
        return f"FUNC{function_code:02X}"

    def _append_history(self, function_code: int, address: int, value: int) -> None:
        key = (function_code, address)
        bucket = self.history.setdefault(key, [])
        bucket.append(value)
        if len(bucket) > MAX_HISTORY_PER_REGISTER:
            del bucket[:-MAX_HISTORY_PER_REGISTER]

    def _purge_stale_requests(self, now: datetime) -> None:
        while self.pending_requests:
            age = (now - self.pending_requests[0].timestamp).total_seconds()
            if age <= MAX_PENDING_AGE_S:
                break
            stale = self.pending_requests.popleft()
            self.stale_requests += 1
            self.logger.line(
                f"[MATCH {now.strftime('%H:%M:%S.%f')[:-3]}] "
                f"Истёк запрос slave=0x{stale.slave_id:02X} fc=0x{stale.function_code:02X} "
                f"addr=0x{stale.address:04X} qty={stale.quantity}"
            )

    def _queue_request(self, frame: ModbusFrame) -> None:
        address = frame.starting_address
        quantity = frame.quantity
        if address is None or quantity is None:
            return
        request = PendingRequest(
            timestamp=frame.timestamp,
            slave_id=frame.slave_id,
            function_code=frame.function_code,
            address=address,
            quantity=quantity,
            raw_bytes=frame.raw_bytes,
        )
        self.pending_requests.append(request)
        self.logger.line(
            f"[MATCH {frame.timestamp.strftime('%H:%M:%S.%f')[:-3]}] "
            f"Запрос поставлен в очередь: slave=0x{frame.slave_id:02X} "
            f"fc=0x{frame.function_code:02X} addr=0x{address:04X} qty={quantity} "
            f"queue={len(self.pending_requests)}"
        )

    def _pop_matching_request(self, frame: ModbusFrame, register_count: int) -> PendingRequest | None:
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

    def _process_decoded_registers(self, frame: ModbusFrame, request: PendingRequest, registers: list[int]) -> None:
        base_address = request.address
        self.snapshots.append(RegisterSnapshot(frame.function_code, base_address, registers[:], frame.timestamp))
        for index, value in enumerate(registers):
            reg_address = base_address + index
            self._append_history(frame.function_code, reg_address, value)
            scales_preview = ", ".join(
                f"x{scale:g}={value * scale:.1f}"
                for scale in TEMPERATURE_SCALES
                if 0 <= value * scale <= 1500
            )
            extra = f" | {scales_preview}" if scales_preview else ""
            pv_mark = " <-- register_pv?" if reg_address == self.furnace.register_pv else ""
            self.logger.line(
                f"[REG   {frame.timestamp.strftime('%H:%M:%S.%f')[:-3]}] "
                f"{self._frame_type_label(frame.function_code)} 0x{reg_address:04X} ({reg_address}): "
                f"{value}{pv_mark}{extra}"
            )

    def process_frame(self, frame: ModbusFrame) -> None:
        self.valid_frames += 1
        self._purge_stale_requests(frame.timestamp)
        time_str = frame.timestamp.strftime("%H:%M:%S.%f")[:-3]

        if frame.is_read_request:
            self._queue_request(frame)
            return

        if frame.is_exception:
            self.logger.line(
                f"[MATCH {time_str}] Exception response slave=0x{frame.slave_id:02X} "
                f"fc=0x{frame.function_code:02X} raw={frame.raw_bytes.hex(' ').upper()}"
            )
            return

        if not frame.is_read_response:
            self.logger.line(
                f"[MATCH {time_str}] Неподдержанный кадр slave=0x{frame.slave_id:02X} "
                f"fc=0x{frame.function_code:02X}"
            )
            return

        registers = frame.get_registers()
        if not registers:
            self.unmatched_responses += 1
            self.logger.line(f"[MATCH {time_str}] Ответ без декодируемых регистров.")
            return

        request = self._pop_matching_request(frame, len(registers))
        if request is None:
            self.unmatched_responses += 1
            self.logger.line(
                f"[MATCH {time_str}] Ответ не сопоставлен: slave=0x{frame.slave_id:02X} "
                f"fc=0x{frame.function_code:02X} regs={len(registers)} queue={len(self.pending_requests)}"
            )
            return

        self.logger.line(
            f"[MATCH {time_str}] Ответ сопоставлен с запросом "
            f"addr=0x{request.address:04X} qty={request.quantity} queue={len(self.pending_requests)}"
        )
        self._process_decoded_registers(frame, request, registers)

    def _candidate_label(self, function_code: int, address: int) -> str:
        return f"{self._frame_type_label(function_code)} 0x{address:04X} ({address})"

    def _build_candidates_for_target(self, target_name: str, target_value: float) -> list[RegisterCandidate]:
        candidates: list[RegisterCandidate] = []
        for (function_code, address), values in self.history.items():
            if not values:
                continue
            avg_raw = sum(values) / len(values)
            best_scale = 1.0
            best_value = avg_raw
            best_error = abs(best_value - target_value)
            for scale in TEMPERATURE_SCALES:
                scaled = avg_raw * scale
                error = abs(scaled - target_value)
                if error < best_error:
                    best_scale = scale
                    best_value = scaled
                    best_error = error
            candidates.append(
                RegisterCandidate(
                    label=self._candidate_label(function_code, address),
                    function_code=function_code,
                    address=address,
                    samples=len(values),
                    avg_raw=avg_raw,
                    min_raw=min(values),
                    max_raw=max(values),
                    last_raw=values[-1],
                    target_name=target_name,
                    target_value=target_value,
                    best_scale=best_scale,
                    best_value=best_value,
                    abs_error=best_error,
                )
            )
        candidates.sort(key=lambda item: (item.abs_error, item.function_code != 4, item.address))
        return candidates

    def _print_target_candidates(self, target_name: str, target_value: float) -> None:
        candidates = self._build_candidates_for_target(target_name, target_value)
        self.logger.section(f"Кандидаты для {target_name}")
        self.logger.line(f"Эталон: {target_value:.1f} C")
        if not candidates:
            self.logger.line("Кандидаты не найдены.")
            return
        for item in candidates[:12]:
            spread = item.max_raw - item.min_raw
            self.logger.line(
                f"{item.label}: avg_raw={item.avg_raw:.2f}, "
                f"scale=x{item.best_scale:g}, approx={item.best_value:.2f} C, "
                f"error={item.abs_error:.2f}, range={item.min_raw}..{item.max_raw}, "
                f"spread={spread}, samples={item.samples}"
            )

    def _print_summary(self) -> None:
        self.logger.section("Итоговая статистика")
        self.logger.line(f"Валидных фреймов: {self.valid_frames}")
        self.logger.line(f"Несопоставленных ответов: {self.unmatched_responses}")
        self.logger.line(f"Просроченных запросов: {self.stale_requests}")
        self.logger.line(f"Очередь pending на конец: {len(self.pending_requests)}")
        self.logger.line(f"Уникальных регистров в истории: {len(self.history)}")
        if self.history:
            self.logger.line("Средние значения по регистрам:")
            for (function_code, address), values in sorted(self.history.items()):
                avg = sum(values) / len(values)
                scales_preview = ", ".join(
                    f"x{scale:g}={avg * scale:.1f}" for scale in TEMPERATURE_SCALES if 0 <= avg * scale <= 1500
                )
                self.logger.line(
                    f"  {self._candidate_label(function_code, address)}: "
                    f"{len(values)} шт., avg_raw={avg:.2f}"
                    + (f" | {scales_preview}" if scales_preview else "")
                )
        if self.chamber_temp is not None:
            self._print_target_candidates("температуры камеры", self.chamber_temp)
        if self.setpoint_temp is not None:
            self._print_target_candidates("уставки", self.setpoint_temp)

    def listen(self, duration_seconds: float) -> None:
        if not self.ser or not self.ser.is_open:
            self.logger.line("Порт не открыт.")
            return

        self.logger.section("RS-485 listener")
        self.logger.line(f"Порт: {self.port}")
        self.logger.line(f"Профиль: {self.profile.name}")
        self.logger.line(f"Параметры линии: {self.profile.wire_label}")
        self.logger.line(f"Длительность: {duration_seconds:.1f} c")
        self.logger.line("Режим: только прослушивание, без передачи в шину.")
        if self.chamber_temp is not None:
            self.logger.line(f"Эталон температуры камеры: {self.chamber_temp:.1f} C")
        if self.setpoint_temp is not None:
            self.logger.line(f"Эталон уставки: {self.setpoint_temp:.1f} C")
        self.logger.line("Парсер: отдельные raw chunk + извлечённые Modbus RTU кадры + matching по очереди запросов.")

        deadline = time.monotonic() + duration_seconds

        try:
            while time.monotonic() < deadline:
                now = time.monotonic()
                waiting = self.ser.in_waiting
                if waiting:
                    chunk = self.ser.read(waiting)
                    if chunk:
                        frames = self.parser.feed(chunk, timestamp=datetime.now(), log_raw=True)
                        for frame in frames:
                            self.process_frame(frame)
                else:
                    time.sleep(0.001)

                if now - self.last_print_time > 5.0:
                    self.logger.line(
                        f"[Статистика] Валидных фреймов: {self.valid_frames}, "
                        f"регистров: {len(self.history)}, pending: {len(self.pending_requests)}, "
                        f"несопоставленных ответов: {self.unmatched_responses}"
                    )
                    self.last_print_time = now
        except KeyboardInterrupt:
            self.logger.line("")
            self.logger.line("Остановлено пользователем.")
        finally:
            remainder = self.parser.drain_remainder()
            if remainder:
                self.logger.line(f"[TAIL] Неразобранный хвост буфера: {remainder.hex(' ').upper()}")
            self._print_summary()
            self.logger.line(f"Итоговый профиль: {self.profile.name} | {self.profile.wire_label}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Прослушивание RS-485 линии и поиск регистров температуры.")
    parser.add_argument("--port", help="COM порт, например COM9 или просто 9")
    parser.add_argument("--profile", choices=["dk518_7e1", "alt_8n1", "custom"], help="Готовый serial-профиль")
    parser.add_argument("--baud", type=int, default=9600, help="Скорость")
    parser.add_argument("--bits", type=int, default=7, choices=[7, 8], help="Биты данных")
    parser.add_argument("--parity", default="E", choices=["N", "E", "O"], help="Чётность")
    parser.add_argument("--stopbits", type=float, default=1.0, help="Стоп-биты")
    parser.add_argument("--duration", type=float, default=90.0, help="Длительность прослушивания в секундах")
    parser.add_argument("--timeout", type=float, default=0.1, help="Serial timeout")
    parser.add_argument("--config", default="config/config.yaml", help="Путь к config.yaml")
    parser.add_argument("--log", help="Явный путь к лог-файлу")
    parser.add_argument("--scale", type=float, help="Переопределить scale_factor")
    parser.add_argument("--chamber-temp", type=float, help="Текущая температура в камере, C")
    parser.add_argument("--setpoint-temp", type=float, help="Текущая уставка, C")
    parser.add_argument("--self-test", action="store_true", help="Прогнать встроенный self-test парсера и выйти")
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if args.self_test:
        ok, messages = run_parser_self_test()
        for message in messages:
            print(message)
        raise SystemExit(0 if ok else 1)

    raw_port = args.port
    if not raw_port:
        raw_port = ask("Введите COM-порт", "COM9")
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

    chamber_temp = args.chamber_temp
    setpoint_temp = args.setpoint_temp
    if chamber_temp is None and setpoint_temp is None:
        print("")
        print("Калибровка по эталонным температурам")
        print("Можно оставить одно поле пустым, если знаете только одну температуру.")
        chamber_temp = ask_optional_float("Текущая температура в камере, C")
        setpoint_temp = ask_optional_float("Текущая уставка, C")

    config = load_furnace_config(Path(args.config))
    if args.scale is not None:
        config.scale_factor = args.scale

    log_path = Path(args.log) if args.log else make_log_path("rs485_listener")
    logger = ConsoleLogger(log_path)
    try:
        logger.line(f"Лог: {log_path}")
        logger.line(f"Порт: {port}")
        logger.line(f"Выбранный профиль: {profile.name}")
        logger.line(f"Параметры линии: {profile.wire_label}")
        logger.line(f"Конфиг: {args.config}")
        if config.known_addresses:
            logger.line(f"Известные адреса из конфига: {[hex(addr) for addr in config.known_addresses]}")
        listener = RS485Listener(
            port=port,
            profile=profile,
            timeout=args.timeout,
            furnace=config,
            logger=logger,
            chamber_temp=chamber_temp,
            setpoint_temp=setpoint_temp,
        )
        if not listener.open():
            raise SystemExit(1)
        try:
            listener.listen(args.duration)
        finally:
            listener.close()
        logger.line("")
        logger.line(f"Файл лога: {log_path}")
    finally:
        logger.close()


if __name__ == "__main__":
    main()
