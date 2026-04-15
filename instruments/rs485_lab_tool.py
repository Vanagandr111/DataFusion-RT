from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

try:
    import serial
    from serial import Serial
except Exception as exc:  # pragma: no cover
    print(f"Не удалось импортировать pyserial: {exc}")
    print("Установите зависимости: python -m pip install -r requirements.txt")
    raise SystemExit(1)


LOG_DIR = Path("logs")


@dataclass(frozen=True)
class SerialProfile:
    baudrate: int
    bytesize: int
    parity: str
    stopbits: float

    @property
    def label(self) -> str:
        stop = int(self.stopbits) if float(self.stopbits).is_integer() else self.stopbits
        return f"{self.baudrate} {self.bytesize}{self.parity}{stop}"


@dataclass
class ProbeResult:
    protocol: str
    method: str
    profile: SerialProfile
    slave_id: int
    function_code: int
    address: int
    status: str
    raw_hex: str = ""
    tx_hex: str = ""
    details: str = ""


COMMON_RTU_PROFILES: list[SerialProfile] = [
    SerialProfile(9600, 7, "E", 1),
    SerialProfile(9600, 8, "E", 1),
    SerialProfile(9600, 8, "N", 1),
    SerialProfile(9600, 7, "O", 1),
    SerialProfile(4800, 7, "E", 1),
    SerialProfile(4800, 8, "E", 1),
    SerialProfile(4800, 8, "N", 1),
]

COMMON_ASCII_PROFILES: list[SerialProfile] = [
    SerialProfile(9600, 7, "E", 1),
    SerialProfile(9600, 8, "N", 1),
    SerialProfile(4800, 7, "E", 1),
    SerialProfile(4800, 8, "N", 1),
]

FULL_PROFILES: list[SerialProfile] = [
    SerialProfile(baudrate, bytesize, parity, 1)
    for baudrate in (2400, 4800, 9600, 19200, 38400)
    for bytesize in (7, 8)
    for parity in ("N", "E", "O")
]

COMMON_SLAVE_IDS = [1, 2, 3, 10]
FULL_SLAVE_IDS = list(range(1, 11))
COMMON_ADDRESSES = [0, 1, 2, 6, 10, 21, 86, 90, 100]
FULL_ADDRESSES = [0, 1, 2, 3, 4, 5, 6, 10, 20, 21, 50, 86, 90, 100]
FUNCTION_CODES = [3, 4]
TX_METHODS = ["plain", "rts"]


def normalize_port(raw: str) -> str:
    value = raw.strip()
    if not value:
        return value
    if value.isdigit():
        return f"COM{value}"
    if value.lower().startswith("com"):
        return value.upper()
    return value


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


def lrc_modbus_ascii(data: bytes) -> int:
    checksum = sum(data) & 0xFF
    return (-checksum) & 0xFF


def build_rtu_request(slave_id: int, function_code: int, address: int, count: int = 1) -> bytes:
    payload = bytes(
        [
            slave_id & 0xFF,
            function_code & 0xFF,
            (address >> 8) & 0xFF,
            address & 0xFF,
            (count >> 8) & 0xFF,
            count & 0xFF,
        ]
    )
    crc = crc16_modbus(payload)
    return payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def build_ascii_request(slave_id: int, function_code: int, address: int, count: int = 1) -> bytes:
    payload = bytes(
        [
            slave_id & 0xFF,
            function_code & 0xFF,
            (address >> 8) & 0xFF,
            address & 0xFF,
            (count >> 8) & 0xFF,
            count & 0xFF,
        ]
    )
    lrc = lrc_modbus_ascii(payload)
    return f":{payload.hex().upper()}{lrc:02X}\r\n".encode("ascii")


def ensure_logs_dir() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR


def make_log_path(prefix: str) -> Path:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return ensure_logs_dir() / f"{prefix}_{ts}.txt"


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


def open_serial(port: str, profile: SerialProfile, *, timeout: float) -> Serial:
    parity_map = {
        "N": serial.PARITY_NONE,
        "E": serial.PARITY_EVEN,
        "O": serial.PARITY_ODD,
    }
    bytesize_map = {
        7: serial.SEVENBITS,
        8: serial.EIGHTBITS,
    }
    stopbits_map = {
        1.0: serial.STOPBITS_ONE,
        1.5: serial.STOPBITS_ONE_POINT_FIVE,
        2.0: serial.STOPBITS_TWO,
    }

    return serial.Serial(
        port=port,
        baudrate=profile.baudrate,
        bytesize=bytesize_map[profile.bytesize],
        parity=parity_map[profile.parity],
        stopbits=stopbits_map[float(profile.stopbits)],
        timeout=timeout,
        write_timeout=timeout,
        rtscts=False,
        dsrdtr=False,
    )


def set_tx_mode(ser: Serial, method: str) -> None:
    if method == "rts":
        ser.rts = True
        ser.dtr = False
        time.sleep(0.010)  # Увеличено с 2 мс до 10 мс
    else:
        ser.rts = False
        ser.dtr = False


def set_rx_mode(ser: Serial, method: str) -> None:
    if method == "rts":
        time.sleep(0.005)  # Увеличено с 1 мс до 5 мс
        ser.rts = False
    ser.dtr = False


def read_available_bytes(ser: Serial, wait_s: float) -> bytes:
    deadline = time.monotonic() + wait_s
    chunks: list[bytes] = []
    last_data_at = 0.0
    while time.monotonic() < deadline:
        waiting = ser.in_waiting
        if waiting:
            data = ser.read(waiting)
            if data:
                chunks.append(data)
                last_data_at = time.monotonic()
        elif last_data_at and (time.monotonic() - last_data_at) > 0.08:
            break
        else:
            time.sleep(0.01)
    return b"".join(chunks)


def analyze_rtu_response(raw: bytes, slave_id: int, function_code: int) -> tuple[str, str]:
    if not raw:
        return "нет ответа", ""
    if len(raw) < 5:
        return "короткий ответ", f"{len(raw)} байт"

    body = raw[:-2]
    crc_expected = int.from_bytes(raw[-2:], "little")
    crc_actual = crc16_modbus(body)
    if crc_expected != crc_actual:
        return "ошибка CRC", f"crc=0x{crc_expected:04X}, ожидалось 0x{crc_actual:04X}"

    if raw[0] != slave_id:
        return "ответ другого slave", f"slave={raw[0]}"
    if raw[1] == (function_code | 0x80):
        exc = raw[2] if len(raw) > 2 else None
        return "исключение Modbus", f"код={exc}"
    if raw[1] != function_code:
        return "другой function code", f"func=0x{raw[1]:02X}"
    return "валидный ответ", ""


def analyze_ascii_response(raw: bytes) -> tuple[str, str]:
    if not raw:
        return "нет ответа", ""
    text = raw.decode("ascii", errors="ignore").strip()
    if not text:
        return "пустой ASCII-ответ", ""
    if not text.startswith(":"):
        return "не ASCII frame", text[:80]

    payload_hex = text[1:]
    if len(payload_hex) < 4 or len(payload_hex) % 2 != 0:
        return "битый ASCII frame", text[:80]
    try:
        payload = bytes.fromhex(payload_hex)
    except ValueError:
        return "невалидный hex", text[:80]
    if len(payload) < 3:
        return "короткий ASCII frame", text[:80]

    body, received_lrc = payload[:-1], payload[-1]
    calc_lrc = lrc_modbus_ascii(body)
    if received_lrc != calc_lrc:
        return "ошибка LRC", f"lrc=0x{received_lrc:02X}, ожидалось 0x{calc_lrc:02X}"
    return "валидный ASCII-ответ", ""


def do_request(
    port: str,
    profile: SerialProfile,
    protocol: str,
    method: str,
    slave_id: int,
    function_code: int,
    address: int,
    *,
    timeout: float,
) -> ProbeResult:
    request = build_rtu_request(slave_id, function_code, address) if protocol == "RTU" else build_ascii_request(slave_id, function_code, address)
    tx_hex = request.hex(" ").upper()
    try:
        ser = open_serial(port, profile, timeout=timeout)
    except Exception as exc:
        return ProbeResult(protocol, method, profile, slave_id, function_code, address, "порт не открылся", tx_hex=tx_hex, details=str(exc))

    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        set_tx_mode(ser, method)
        ser.write(request)
        ser.flush()
        set_rx_mode(ser, method)
        time.sleep(0.15)  # Увеличено с 50 мс до 150 мс
        raw = read_available_bytes(ser, timeout)
    except Exception as exc:
        return ProbeResult(protocol, method, profile, slave_id, function_code, address, "ошибка обмена", tx_hex=tx_hex, details=str(exc))
    finally:
        try:
            ser.close()
        except Exception:
            pass

    raw_hex = raw.hex(" ").upper()
    if protocol == "RTU":
        status, details = analyze_rtu_response(raw, slave_id, function_code)
    else:
        status, details = analyze_ascii_response(raw)

    return ProbeResult(protocol, method, profile, slave_id, function_code, address, status, raw_hex, tx_hex, details)


def print_result(logger: ConsoleLogger, result: ProbeResult) -> None:
    line = (
        f"[{result.protocol:<5}] "
        f"{result.profile.label:<12} "
        f"{result.method:<5} "
        f"slave={result.slave_id:<2} "
        f"fc={result.function_code:<2} "
        f"addr={result.address:<4} -> "
        f"{result.status}"
    )
    if result.details:
        line += f" | {result.details}"
    if result.tx_hex:
        line += f" | TX: {result.tx_hex}"
    if result.raw_hex:
        line += f" | RX: {result.raw_hex}"
    logger.line(line)


def build_single_profile(
    *,
    baudrate: int | None,
    bytesize: int | None,
    parity: str | None,
    stopbits: float | None,
) -> SerialProfile | None:
    if baudrate is None and bytesize is None and parity is None and stopbits is None:
        return None
    return SerialProfile(
        baudrate or 9600,
        bytesize or 8,
        (parity or "N").upper(),
        stopbits or 1.0,
    )


def run_probe_suite(
    port: str,
    logger: ConsoleLogger,
    *,
    forced_profile: SerialProfile | None = None,
) -> None:
    logger.section("RS-485 / PID brute-force probe")
    logger.line(f"Порт: {port}")
    logger.line("Порядок: сначала RTU common, затем ASCII common, потом расширенный перебор.")
    logger.line("Probe: MCGS лучше физически отключить, чтобы не было конфликта двух master-устройств.")
    if forced_profile is not None:
        logger.line(f"Зафиксированный serial-профиль: {forced_profile.label}")

    results: list[ProbeResult] = []
    common_rtu_profiles = [forced_profile] if forced_profile else COMMON_RTU_PROFILES
    common_ascii_profiles = [forced_profile] if forced_profile else COMMON_ASCII_PROFILES

    logger.section("Этап 1: RTU common")
    for profile in common_rtu_profiles:
        for method in TX_METHODS:
            for slave_id in COMMON_SLAVE_IDS:
                for function_code in FUNCTION_CODES:
                    for address in COMMON_ADDRESSES:
                        result = do_request(
                            port,
                            profile,
                            "RTU",
                            method,
                            slave_id,
                            function_code,
                            address,
                            timeout=1.2 if forced_profile else 0.7,  # Увеличено таймауты
                        )
                        results.append(result)
                        print_result(logger, result)

    logger.section("Этап 2: ASCII common")
    for profile in common_ascii_profiles:
        for method in TX_METHODS:
            for slave_id in COMMON_SLAVE_IDS:
                for function_code in FUNCTION_CODES:
                    for address in COMMON_ADDRESSES:
                        result = do_request(
                            port,
                            profile,
                            "ASCII",
                            method,
                            slave_id,
                            function_code,
                            address,
                            timeout=1.4 if forced_profile else 0.9,  # Увеличено таймауты
                        )
                        results.append(result)
                        print_result(logger, result)

    if forced_profile is None:
        logger.section("Этап 3: RTU расширенный перебор")
        attempts = 0
        for profile in FULL_PROFILES:
            if profile in COMMON_RTU_PROFILES:
                continue
            logger.line(f"Профиль: {profile.label}")
            for method in TX_METHODS:
                for slave_id in FULL_SLAVE_IDS:
                    for function_code in FUNCTION_CODES:
                        for address in FULL_ADDRESSES:
                            result = do_request(port, profile, "RTU", method, slave_id, function_code, address, timeout=0.4)  # Увеличено
                            results.append(result)
                            attempts += 1
                            if attempts % 500 == 0:
                                logger.line(f"Прогресс: {attempts} запросов без остановки.")
                            if result.status != "нет ответа":
                                print_result(logger, result)

        logger.section("Этап 4: ASCII расширенный перебор")
        attempts = 0
        for profile in FULL_PROFILES:
            if profile in COMMON_ASCII_PROFILES:
                continue
            logger.line(f"Профиль: {profile.label}")
            for method in TX_METHODS:
                for slave_id in FULL_SLAVE_IDS:
                    for function_code in FUNCTION_CODES:
                        for address in FULL_ADDRESSES:
                            result = do_request(port, profile, "ASCII", method, slave_id, function_code, address, timeout=0.5)  # Увеличено
                            results.append(result)
                            attempts += 1
                            if attempts % 500 == 0:
                                logger.line(f"Прогресс: {attempts} запросов без остановки.")
                            if result.status != "нет ответа":
                                print_result(logger, result)
    else:
        logger.section("Этап 3-4: расширенный перебор пропущен")
        logger.line("Так как задан точный serial-профиль, выполнены только common-проверки RTU и ASCII.")

    summarize_results(results, logger)


def summarize_results(results: Iterable[ProbeResult], logger: ConsoleLogger) -> None:
    results = list(results)
    logger.section("Итог")
    interesting = [item for item in results if item.status not in {"нет ответа", "порт не открылся"}]
    if not interesting:
        logger.line("Ничего, кроме таймаутов, не найдено.")
    else:
        logger.line(f"Найдено интересных ответов: {len(interesting)}")
        for item in interesting[:30]:
            print_result(logger, item)
        if len(interesting) > 30:
            logger.line(f"... ещё {len(interesting) - 30} строк см. в файле лога.")


def sniff_with_profile(port: str, profile: SerialProfile, logger: ConsoleLogger, *, seconds: float) -> None:
    logger.section(f"Сниффинг {profile.label}")
    logger.line("Только чтение. Ничего в линию не отправляется.")
    try:
        ser = open_serial(port, profile, timeout=0.10)
    except Exception as exc:
        logger.line(f"Не удалось открыть порт: {exc}")
        return

    try:
        ser.rts = False
        ser.dtr = False
        deadline = time.monotonic() + seconds
        chunk = bytearray()
        last_data_at = 0.0
        last_packet_time: datetime | None = None
        while time.monotonic() < deadline:
            waiting = ser.in_waiting
            if waiting:
                data = ser.read(waiting)
                if data:
                    chunk.extend(data)
                    last_data_at = time.monotonic()
            elif chunk and (time.monotonic() - last_data_at) > 0.08:
                now = datetime.now()
                delta_ms = None
                if last_packet_time is not None:
                    delta_ms = (now - last_packet_time).total_seconds() * 1000
                dump_chunk(bytes(chunk), logger, now, delta_ms)
                last_packet_time = now
                chunk.clear()
            else:
                time.sleep(0.01)
        if chunk:
            now = datetime.now()
            delta_ms = None
            if last_packet_time is not None:
                delta_ms = (now - last_packet_time).total_seconds() * 1000
            dump_chunk(bytes(chunk), logger, now, delta_ms)
    finally:
        ser.close()


def dump_chunk(data: bytes, logger: ConsoleLogger, timestamp: datetime | None = None, delta_ms: float | None = None) -> None:
    if timestamp is None:
        timestamp = datetime.now()
    time_str = timestamp.strftime("%H:%M:%S.%f")[:-3]
    
    hex_str = data.hex(" ").upper()
    ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    
    logger.line(f"TIME : {time_str}")
    if delta_ms is not None:
        logger.line(f"DELTA: +{delta_ms:.1f} ms")
    logger.line(f"HEX  : {hex_str}")
    logger.line(f"ASCII: {ascii_str}")
    logger.line("-" * 72)


def run_sniffer(
    port: str,
    logger: ConsoleLogger,
    *,
    forced_profile: SerialProfile | None = None,
    duration: float = 8.0,
) -> None:
    logger.section("RS-485 sniff mode")
    logger.line(f"Порт: {port}")
    logger.line("Sniff: MCGS лучше оставить подключённым, а свой адаптер использовать только как слушатель.")
    profiles = (
        [forced_profile]
        if forced_profile
        else [
            SerialProfile(9600, 7, "E", 1),
            SerialProfile(9600, 8, "N", 1),
            SerialProfile(9600, 8, "E", 1),
            SerialProfile(19200, 8, "N", 1),
            SerialProfile(38400, 8, "N", 1),
            SerialProfile(4800, 7, "E", 1),
        ]
    )
    if forced_profile:
        logger.line(f"Зафиксированный serial-профиль: {forced_profile.label}")
    else:
        logger.line("Сначала common-профили, потом при желании можно повторить вручную.")
    for profile in profiles:
        sniff_with_profile(port, profile, logger, seconds=duration)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def interactive_main() -> int:
    port = normalize_port(ask("Введите COM-порт", "COM9")).upper()
    print("")
    print("Выберите режим:")
    print("1 - Брутфорс RTU + ASCII")
    print("2 - Сниффинг линии")
    print("0 - Выход")
    mode = ask("Режим", "1")

    if mode == "0":
        print("Выход.")
        return 0

    prefix = "rs485_sniff" if mode == "2" else "rs485_bruteforce"
    log_path = make_log_path(prefix)
    logger = ConsoleLogger(log_path)
    logger.line(f"Лог: {log_path}")
    logger.line(f"Порт: {port}")
    try:
        if mode == "2":
            run_sniffer(port, logger)
        else:
            run_probe_suite(port, logger)
    except KeyboardInterrupt:
        logger.line("")
        logger.line("Остановлено пользователем.")
    finally:
        logger.line("")
        logger.line(f"Файл лога: {log_path}")
        logger.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Консольный инструмент для перебора RTU/ASCII и сниффинга RS-485 линии.")
    parser.add_argument("--port", help="COM-порт, например COM9")
    parser.add_argument("--mode", choices=["probe", "sniff"], help="Режим запуска")
    parser.add_argument("--baud", type=int, help="Фиксированный baudrate, например 9600")
    parser.add_argument("--bits", type=int, choices=[7, 8], help="Фиксированный размер слова: 7 или 8 бит")
    parser.add_argument("--parity", choices=["N", "E", "O", "n", "e", "o"], help="Фиксированная чётность")
    parser.add_argument("--stopbits", type=float, choices=[1.0, 1.5, 2.0], help="Фиксированные стоп-биты")
    parser.add_argument("--duration", type=float, default=8.0, help="Длительность одного снифф-сеанса в секундах")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    forced_profile = build_single_profile(
        baudrate=args.baud,
        bytesize=args.bits,
        parity=args.parity.upper() if args.parity else None,
        stopbits=args.stopbits,
    )

    if not args.port or not args.mode:
        return interactive_main()

    port = normalize_port(args.port)
    log_path = make_log_path("rs485_cli")
    logger = ConsoleLogger(log_path)
    logger.line(f"Лог: {log_path}")
    logger.line(f"Порт: {port}")
    try:
        if args.mode == "sniff":
            run_sniffer(port, logger, forced_profile=forced_profile, duration=args.duration)
        else:
            run_probe_suite(port, logger, forced_profile=forced_profile)
    finally:
        logger.line("")
        logger.line(f"Файл лога: {log_path}")
        logger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
