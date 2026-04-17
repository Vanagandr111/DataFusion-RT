from __future__ import annotations

import inspect
import logging

try:
    from pymodbus.client import ModbusSerialClient
except Exception:  # pragma: no cover - import fallback for missing dependency
    ModbusSerialClient = None  # type: ignore[assignment]

from app.models import FurnaceConfig


def open_modbus_serial_client(
    config: FurnaceConfig,
    *,
    logger: logging.Logger,
    device_label: str,
) -> ModbusSerialClient | None:
    if ModbusSerialClient is None:
        logger.error("Библиотека pymodbus не установлена. Чтение %s отключено.", device_label)
        return None

    client = ModbusSerialClient(
        port=config.port,
        baudrate=config.baudrate,
        bytesize=config.bytesize,
        parity=config.parity,
        stopbits=config.stopbits,
        timeout=config.timeout,
    )
    if not client.connect():
        logger.warning("Не удалось открыть Modbus-порт %s: %s", device_label, config.port)
        client.close()
        return None
    logger.info("Подключение к %s открыто на %s", device_label, config.port)
    return client


def resolve_modbus_device_arg_name(
    client,
    cached_name: str | None,
) -> str | None:
    if cached_name is not None:
        return cached_name
    if client is None:
        return None

    try:
        params = inspect.signature(client.read_holding_registers).parameters
    except (TypeError, ValueError):
        params = {}

    for candidate in ("device_id", "slave", "unit"):
        if candidate in params:
            return candidate
    return None
