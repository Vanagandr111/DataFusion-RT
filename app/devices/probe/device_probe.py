from __future__ import annotations

import logging
import time

try:
    import serial
except Exception:  # pragma: no cover - optional dependency in test environment
    serial = None  # type: ignore[assignment]

from app.devices.models.status import ProbeResult
from app.models import FurnaceConfig, ScaleConfig
from app.devices.readers.furnace_reader import FurnaceReader
from app.devices.readers.passive_furnace_reader import PassiveFurnaceReader
from app.utils.parsers import parse_mass_line


def probe_scale(
    config: ScaleConfig,
    *,
    test_mode: bool,
    logger: logging.Logger,
) -> ProbeResult:
    if test_mode:
        return ProbeResult(True, "Тестовый режим: виртуальные весы доступны.", config.port.strip(), "scale")

    if serial is None:
        logger.error("Библиотека pyserial не установлена. Проверка весов недоступна.")
        return ProbeResult(False, "Библиотека pyserial не установлена.", config.port.strip(), "scale")

    port_name = config.port.strip()
    if not port_name:
        logger.warning("Проверка весов пропущена: COM-порт не выбран.")
        return ProbeResult(False, "COM-порт для весов не выбран.", port_name, "scale")

    serial_conn: object | None = None
    try:
        serial_conn = serial.Serial(
            port=port_name,
            baudrate=config.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=max(0.35, config.timeout),
            write_timeout=max(0.35, config.timeout),
        )

        candidates: list[str] = []
        for _ in range(3):
            raw = serial_conn.read_until(expected=b"\n", size=128)
            text = raw.decode("ascii", errors="ignore").strip()
            if text:
                candidates.append(text)
                parsed = parse_mass_line(text, logger=logger)
                if parsed is not None:
                    return ProbeResult(True, f"Весы обнаружены на {port_name}: {parsed:.3f} г", port_name, "scale")

        serial_conn.write(config.request_command.encode("ascii"))
        serial_conn.flush()
        time.sleep(0.15)

        for _ in range(4):
            raw = serial_conn.read_until(expected=b"\n", size=128)
            text = raw.decode("ascii", errors="ignore").strip()
            if text:
                candidates.append(text)
                parsed = parse_mass_line(text, logger=logger)
                if parsed is not None:
                    return ProbeResult(True, f"Похоже на весы на {port_name}: {parsed:.3f} г", port_name, "scale")

        if candidates:
            logger.warning("Порт %s открылся, но масса не распознана: %r", port_name, candidates[-1])
            return ProbeResult(False, f"Порт {port_name} открыт, но ответ весов не удалось распознать.", port_name, "scale", details=candidates[-1])

        logger.warning("Порт %s открылся, но весы не прислали данные.", port_name)
        return ProbeResult(False, f"Порт {port_name} открыт, но весы не прислали данные.", port_name, "scale")
    except Exception as exc:
        if serial is not None and isinstance(exc, serial.SerialException):
            logger.warning("Не удалось открыть %s для проверки весов: %s", port_name, exc)
            return ProbeResult(False, f"Не удалось открыть {port_name}: {exc}", port_name, "scale", details=str(exc))
        if isinstance(exc, OSError):
            logger.warning("Ошибка порта %s при проверке весов: %s", port_name, exc)
            return ProbeResult(False, f"Ошибка порта {port_name}: {exc}", port_name, "scale", details=str(exc))
        logger.warning("Не удалось открыть %s для проверки весов: %s", port_name, exc)
        return ProbeResult(False, f"Не удалось открыть {port_name}: {exc}", port_name, "scale", details=str(exc))
    finally:
        if serial_conn is not None and getattr(serial_conn, "is_open", False):
            serial_conn.close()


def probe_furnace(
    config: FurnaceConfig,
    *,
    test_mode: bool,
    logger: logging.Logger,
) -> ProbeResult:
    if test_mode:
        return ProbeResult(True, "Тестовый режим: виртуальная печь доступна.", config.port.strip(), "furnace")

    port_name = config.port.strip()
    if not port_name:
        logger.warning("Проверка печи пропущена: COM-порт не выбран.")
        return ProbeResult(False, "COM-порт для печи не выбран.", port_name, "furnace")

    driver = (config.driver or "modbus").lower()
    if driver == "dk518":
        reader = PassiveFurnaceReader(config, test_mode=False, logger=logger.getChild("probe"))
        deadline = time.monotonic() + max(2.5, config.timeout * 8.0)
        try:
            while time.monotonic() < deadline:
                pv, sv = reader.read_temperatures()
                if pv is not None:
                    return ProbeResult(True, f"Печь обнаружена на {port_name}: камера {pv:.2f} °C (пассивное прослушивание)", port_name, "furnace")
                if sv is not None:
                    return ProbeResult(True, f"Печь обнаружена на {port_name}: термопара {sv:.2f} °C (пассивное прослушивание)", port_name, "furnace")
                time.sleep(0.05)
            logger.warning("Во время пассивной проверки печи на %s не обнаружен RS-485 трафик.", port_name)
            return ProbeResult(False, f"На {port_name} не замечен трафик печи. Пассивная проверка ничего не услышала.", port_name, "furnace")
        finally:
            reader.close()

    reader = FurnaceReader(config, test_mode=False, logger=logger.getChild("probe"))
    try:
        pv = reader.read_pv()
        if pv is not None:
            return ProbeResult(True, f"Печь обнаружена на {port_name}: текущая температура {pv:.2f} °C", port_name, "furnace")

        sv = reader.read_sv()
        if sv is not None:
            return ProbeResult(True, f"Печь обнаружена на {port_name}: заданная температура {sv:.2f} °C", port_name, "furnace")

        logger.warning("Нет Modbus-ответа от %s во время проверки печи.", port_name)
        return ProbeResult(False, f"Нет ответа Modbus от {port_name}. Проверьте адрес устройства и регистры.", port_name, "furnace")
    finally:
        reader.close()


def probe_scale_port(
    config: ScaleConfig,
    *,
    test_mode: bool,
    logger: logging.Logger,
) -> tuple[bool, str]:
    return probe_scale(config, test_mode=test_mode, logger=logger).as_tuple()


def probe_furnace_port(
    config: FurnaceConfig,
    *,
    test_mode: bool,
    logger: logging.Logger,
) -> tuple[bool, str]:
    return probe_furnace(config, test_mode=test_mode, logger=logger).as_tuple()
