from __future__ import annotations

import serial

from app.models import FurnaceConfig, ScaleConfig


def open_scale_serial(config: ScaleConfig) -> serial.Serial:
    return serial.Serial(
        port=config.port,
        baudrate=config.baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=config.timeout,
        write_timeout=config.timeout,
    )


def open_passive_furnace_serial(config: FurnaceConfig) -> serial.Serial:
    parity_map = {
        "N": serial.PARITY_NONE,
        "E": serial.PARITY_EVEN,
        "O": serial.PARITY_ODD,
    }
    bytesize_map = {7: serial.SEVENBITS, 8: serial.EIGHTBITS}
    stopbits_map = {
        1.0: serial.STOPBITS_ONE,
        1.5: serial.STOPBITS_ONE_POINT_FIVE,
        2.0: serial.STOPBITS_TWO,
    }
    connection = serial.Serial(
        port=config.port,
        baudrate=config.baudrate,
        bytesize=bytesize_map[config.bytesize],
        parity=parity_map[config.parity],
        stopbits=stopbits_map[float(config.stopbits)],
        timeout=max(0.05, float(config.timeout)),
    )
    connection.rts = False
    connection.dtr = False
    return connection
