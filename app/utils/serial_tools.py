from __future__ import annotations

from serial.tools import list_ports

from app.models import PortInfo


def port_display_label(port: PortInfo) -> str:
    return f"{port.device} - {port.description}"


def guess_port_kind(port: PortInfo) -> str:
    haystack = f"{port.device} {port.description} {port.hwid}".lower()

    if "bluetooth" in haystack or "bthenum" in haystack:
        return "Bluetooth"
    if any(token in haystack for token in ("ftdi", "ch340", "cp210", "silicon labs", "pl2303", "prolific")):
        return "USB-Serial адаптер"
    if any(token in haystack for token in ("usb serial", "wch", "rs-485", "rs485", "uart", "serial device")):
        return "USB / UART / RS-485"
    if any(token in haystack for token in ("arduino", "cdc", "virtual com")):
        return "USB CDC устройство"
    return "Другое устройство"


def list_available_ports() -> list[PortInfo]:
    ports: list[PortInfo] = []
    for item in list_ports.comports():
        ports.append(
            PortInfo(
                device=item.device,
                description=item.description or "n/a",
                hwid=item.hwid or "n/a",
            )
        )
    return ports


def format_port_listing(ports: list[PortInfo]) -> str:
    if not ports:
        return "COM-порты не обнаружены."

    lines = ["Обнаруженные COM-порты:"]
    for port in ports:
        lines.append(
            f"  {port.device} | {guess_port_kind(port)} | {port.description} | {port.hwid}"
        )
    return "\n".join(lines)


def port_exists(port_name: str) -> bool:
    wanted = port_name.strip().upper()
    return any(port.device.strip().upper() == wanted for port in list_available_ports())
