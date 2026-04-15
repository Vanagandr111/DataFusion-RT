from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


LOG_DIR = Path("logs")
SCALES = (1.0, 0.1, 0.01)
SUMMARY_RE = re.compile(
    r"^\s+(HOLD|INPUT)\s+0x([0-9A-Fa-f]{4})\s+\((\d+)\):\s+(\d+)\s+шт\.,\s+avg_raw=([0-9.]+)"
)


@dataclass
class RegisterSummary:
    reg_type: str
    address: int
    samples: int
    avg_raw: float


def ask(prompt: str) -> str:
    return input(f"{prompt}: ").strip()


def list_logs() -> list[Path]:
    return sorted(LOG_DIR.glob("rs485_listener_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)


def parse_log(path: Path) -> dict[tuple[str, int], RegisterSummary]:
    result: dict[tuple[str, int], RegisterSummary] = {}
    text = path.read_text(encoding="utf-8-sig")
    for line in text.splitlines():
        match = SUMMARY_RE.match(line)
        if not match:
            continue
        reg_type = match.group(1)
        address = int(match.group(3))
        samples = int(match.group(4))
        avg_raw = float(match.group(5))
        result[(reg_type, address)] = RegisterSummary(reg_type, address, samples, avg_raw)
    return result


def format_scaled(value: float) -> str:
    parts = [f"x{scale:g}={value * scale:.2f}" for scale in SCALES]
    return ", ".join(parts)


def compare_logs(path_a: Path, path_b: Path) -> str:
    data_a = parse_log(path_a)
    data_b = parse_log(path_b)
    keys = sorted(set(data_a) & set(data_b), key=lambda item: (item[0], item[1]))

    lines: list[str] = []
    lines.append(f"Сравнение логов:")
    lines.append(f"1. {path_a.name}")
    lines.append(f"2. {path_b.name}")
    lines.append("")

    changed: list[tuple[float, str]] = []
    same_count = 0
    for key in keys:
        left = data_a[key]
        right = data_b[key]
        delta_raw = right.avg_raw - left.avg_raw
        if abs(delta_raw) < 1e-9:
            same_count += 1
            continue
        delta_text = ", ".join(f"x{scale:g}={delta_raw * scale:+.2f}" for scale in SCALES)
        changed.append(
            (
                abs(delta_raw),
                f"{left.reg_type} 0x{left.address:04X} ({left.address}): "
                f"{left.avg_raw:.2f} -> {right.avg_raw:.2f} | "
                f"delta_raw={delta_raw:+.2f} | {delta_text}",
            )
        )

    changed.sort(key=lambda item: item[0], reverse=True)
    lines.append(f"Общих регистров: {len(keys)}")
    lines.append(f"Не изменились: {same_count}")
    lines.append(f"Изменились: {len(changed)}")
    lines.append("")

    if changed:
        lines.append("Регистры с изменениями:")
        for _, line in changed:
            lines.append(line)
    else:
        lines.append("Изменений между логами не найдено.")

    only_a = sorted(set(data_a) - set(data_b), key=lambda item: (item[0], item[1]))
    only_b = sorted(set(data_b) - set(data_a), key=lambda item: (item[0], item[1]))
    if only_a:
        lines.append("")
        lines.append(f"Есть только в {path_a.name}:")
        for reg_type, address in only_a:
            item = data_a[(reg_type, address)]
            lines.append(f"{reg_type} 0x{address:04X} ({address}): avg_raw={item.avg_raw:.2f}")
    if only_b:
        lines.append("")
        lines.append(f"Есть только в {path_b.name}:")
        for reg_type, address in only_b:
            item = data_b[(reg_type, address)]
            lines.append(f"{reg_type} 0x{address:04X} ({address}): avg_raw={item.avg_raw:.2f}")
    return "\n".join(lines)


def main() -> int:
    logs = list_logs()
    if len(logs) < 2:
        print("Для сравнения нужно минимум два лога rs485_listener_*.txt в папке logs.")
        return 1

    print("Доступные логи:")
    for index, path in enumerate(logs, start=1):
        print(f"{index:>2}. {path.name}")

    raw = ask("Введите два номера через пробел")
    parts = raw.split()
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        print("Нужно ввести ровно два номера, например: 1 3")
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

    out_path = LOG_DIR / f"rs485_compare_{path_a.stem}__VS__{path_b.stem}.txt"
    out_path.write_text(report + "\n", encoding="utf-8-sig")
    print()
    print(f"Отчёт сохранён: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
