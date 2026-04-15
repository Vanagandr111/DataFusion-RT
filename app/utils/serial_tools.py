from __future__ import annotations

from serial.tools import list_ports

from app.models import PortInfo


def port_display_label(port: PortInfo) -> str:
    return f"{port.device} - {port.description}"


def guess_port_kind(port: PortInfo) -> str:
    haystack = f"{port.device} {port.description} {port.hwid}".lower()

    if "bluetooth" in haystack or "bthenum" in haystack:
        return "Bluetooth"
    if any(
        token in haystack
        for token in ("ftdi", "ch340", "cp210", "silicon labs", "pl2303", "prolific")
    ):
        return "USB-Serial адаптер"
    if any(
        token in haystack
        for token in ("usb serial", "wch", "rs-485", "rs485", "uart", "serial device")
    ):
        return "USB / UART / RS-485"
    if any(token in haystack for token in ("arduino", "cdc", "virtual com")):
        return "USB CDC устройство"
    return "Другое устройство"


def detect_preferred_ports(ports: list[PortInfo]) -> dict[str, PortInfo]:
    furnace_candidates: list[tuple[int, PortInfo]] = []
    scale_candidates: list[tuple[int, PortInfo]] = []

    for port in ports:
        haystack = f"{port.device} {port.description} {port.hwid}".lower()
        furnace_score = 0
        scale_score = 0

        if any(
            token in haystack
            for token in (
                "rs-485",
                "rs485",
                "485",
                "usb-serial",
                "usb serial",
                "uart",
                "wch",
                "ch340",
                "qinheng",
                "ftdi",
            )
        ):
            furnace_score += 3
        if any(token in haystack for token in ("modbus", "converter", "adapter")):
            furnace_score += 2
        if "bluetooth" in haystack:
            furnace_score -= 5

        if any(
            token in haystack
            for token in (
                "cp210",
                "silicon labs",
                "adam",
                "highland",
                "hcb",
                "balance",
                "scale",
                "weigh",
            )
        ):
            scale_score += 3
        if any(
            token in haystack
            for token in (
                "usb serial",
                "virtual com",
                "cdc",
                "usb/ uart/rs-485",
                "usb serial port",
            )
        ):
            scale_score += 2
        if any(
            token in haystack
            for token in ("ch340", "wch", "ftdi", "uart", "serial port")
        ):
            scale_score += 1

        if furnace_score > 0:
            furnace_candidates.append((furnace_score, port))
        if scale_score > 0:
            scale_candidates.append((scale_score, port))

    result: dict[str, PortInfo] = {}
    used_devices: set[str] = set()

    if furnace_candidates:
        furnace_candidates.sort(key=lambda item: (-item[0], item[1].device))
        result["furnace"] = furnace_candidates[0][1]
        used_devices.add(furnace_candidates[0][1].device.upper())

    if scale_candidates:
        scale_candidates.sort(key=lambda item: (-item[0], item[1].device))
        for _score, port in scale_candidates:
            if port.device.upper() not in used_devices:
                result["scale"] = port
                used_devices.add(port.device.upper())
                break

    if "furnace" not in result:
        for port in ports:
            haystack = f"{port.device} {port.description} {port.hwid}".lower()
            if any(
                token in haystack
                for token in ("ch340", "wch", "rs-485", "rs485", "usb serial", "uart")
            ):
                result["furnace"] = port
                used_devices.add(port.device.upper())
                break

    if "scale" not in result:
        for port in ports:
            if port.device.upper() not in used_devices:
                result["scale"] = port
                break

    return result


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
