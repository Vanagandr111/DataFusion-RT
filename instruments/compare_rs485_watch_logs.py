from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


LOG_DIR = Path("logs")
REGISTER_RE = re.compile(r"^Регистр:\s+0x([0-9A-Fa-f]{4})\s+\((\d+)\)")
SCALE_RE = re.compile(r"^Scale:\s+x([0-9.]+)")
OBS_RE = re.compile(r"^Наблюдений:\s+(\d+)")
RAW_STATS_RE = re.compile(r"^raw min/avg/max:\s+([0-9.]+)\s+/\s+([0-9.]+)\s+/\s+([0-9.]+)")
SCALED_STATS_RE = re.compile(r"^scaled min/avg/max:\s+([0-9.]+)\s+/\s+([0-9.]+)\s+/\s+([0-9.]+)")
SCREEN_RE = re.compile(r"^screen-rounded unique:\s+\[(.*)\]")


@dataclass
class WatchSummary:
    path: Path
    address: int
    scale: float
    observations: int
    raw_min: float
    raw_avg: float
    raw_max: float
    scaled_min: float
    scaled_avg: float
    scaled_max: float
    screen_values: list[int]


def ask(prompt: str) -> str:
    return input(f"{prompt}: ").strip()


def list_logs() -> list[Path]:
    return sorted(LOG_DIR.glob("rs485_watch_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)


def parse_screen_values(raw: str) -> list[int]:
    text = raw.strip()
    if not text:
        return []
    return [int(part.strip()) for part in text.split(",") if part.strip()]


def parse_log(path: Path) -> WatchSummary | None:
    address = None
    scale = None
    observations = None
    raw_min = raw_avg = raw_max = None
    scaled_min = scaled_avg = scaled_max = None
    screen_values: list[int] = []

    text = path.read_text(encoding="utf-8-sig")
    for line in text.splitlines():
        if address is None:
            match = REGISTER_RE.match(line)
            if match:
                address = int(match.group(2))
                continue
        if scale is None:
            match = SCALE_RE.match(line)
            if match:
                scale = float(match.group(1))
                continue
        if observations is None:
            match = OBS_RE.match(line)
            if match:
                observations = int(match.group(1))
                continue
        if raw_avg is None:
            match = RAW_STATS_RE.match(line)
            if match:
                raw_min = float(match.group(1))
                raw_avg = float(match.group(2))
                raw_max = float(match.group(3))
                continue
        if scaled_avg is None:
            match = SCALED_STATS_RE.match(line)
            if match:
                scaled_min = float(match.group(1))
                scaled_avg = float(match.group(2))
                scaled_max = float(match.group(3))
                continue
        match = SCREEN_RE.match(line)
        if match:
            screen_values = parse_screen_values(match.group(1))

    if (
        address is None
        or scale is None
        or observations is None
        or raw_min is None
        or raw_avg is None
        or raw_max is None
        or scaled_min is None
        or scaled_avg is None
        or scaled_max is None
    ):
        return None

    return WatchSummary(
        path=path,
        address=address,
        scale=scale,
        observations=observations,
        raw_min=raw_min,
        raw_avg=raw_avg,
        raw_max=raw_max,
        scaled_min=scaled_min,
        scaled_avg=scaled_avg,
        scaled_max=scaled_max,
        screen_values=screen_values,
    )


def compare_logs(path_a: Path, path_b: Path) -> str:
    left = parse_log(path_a)
    right = parse_log(path_b)
    if left is None or right is None:
        missing = path_a.name if left is None else path_b.name
        return f"Не удалось распарсить лог: {missing}"

    lines: list[str] = []
    lines.append("Сравнение watch-логов:")
    lines.append(f"1. {path_a.name}")
    lines.append(f"2. {path_b.name}")
    lines.append("")
    lines.append(f"Регистр #1: 0x{left.address:04X} ({left.address})")
    lines.append(f"Регистр #2: 0x{right.address:04X} ({right.address})")
    lines.append(f"Scale #1: x{left.scale:g}")
    lines.append(f"Scale #2: x{right.scale:g}")
    lines.append("")

    if left.address != right.address:
        lines.append("Внимание: сравниваются разные адреса регистров.")
        lines.append("")

    delta_raw_avg = right.raw_avg - left.raw_avg
    delta_scaled_avg = right.scaled_avg - left.scaled_avg

    lines.append(f"Наблюдений: {left.observations} -> {right.observations}")
    lines.append(
        f"raw avg: {left.raw_avg:.2f} -> {right.raw_avg:.2f} | delta={delta_raw_avg:+.2f}"
    )
    lines.append(
        f"scaled avg: {left.scaled_avg:.2f} -> {right.scaled_avg:.2f} | delta={delta_scaled_avg:+.2f}"
    )
    lines.append(f"raw min/max #1: {left.raw_min:.2f} .. {left.raw_max:.2f}")
    lines.append(f"raw min/max #2: {right.raw_min:.2f} .. {right.raw_max:.2f}")
    lines.append(f"screen-rounded #1: {left.screen_values}")
    lines.append(f"screen-rounded #2: {right.screen_values}")
    return "\n".join(lines)


def main() -> int:
    logs = list_logs()
    if len(logs) < 2:
        print("Для сравнения нужно минимум два лога rs485_watch_*.txt в папке logs.")
        return 1

    print("Доступные watch-логи:")
    for index, path in enumerate(logs, start=1):
        print(f"{index:>2}. {path.name}")

    raw = ask("Введите два номера через пробел")
    parts = raw.split()
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        print("Нужно ввести ровно два номера, например: 1 2")
        return 1

    first_idx, second_idx = (int(part) for part in parts)
    if not (1 <= first_idx <= len(logs) and 1 <= second_idx <= len(logs)):
        print("Один из номеров вне диапазона.")
        return 1
    if first_idx == second_idx:
        print("Нужно выбрать два разных лога.")
        return 1

    path_a = logs[first_idx - 1]
    path_b = logs[second_idx - 1]
    report = compare_logs(path_a, path_b)
    print()
    print(report)

    out_path = LOG_DIR / f"rs485_watch_compare_{path_a.stem}__VS__{path_b.stem}.txt"
    out_path.write_text(report + "\n", encoding="utf-8-sig")
    print()
    print(f"Отчёт сохранён: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
