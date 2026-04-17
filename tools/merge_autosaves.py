from __future__ import annotations

import argparse
import json
from collections import OrderedDict
from pathlib import Path


def load_records(path: Path) -> tuple[dict, list[dict]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload, list(payload.get("records", []))


def clean_record(record: dict) -> dict:
    cleaned = dict(record)
    mass = cleaned.get("mass")
    if mass == 4 or mass == 4.0:
        cleaned["mass"] = None
    return cleaned


def should_skip_record(record: dict) -> bool:
    mass = record.get("mass")
    if isinstance(mass, (int, float)):
        return mass < 0
    if isinstance(mass, str):
        try:
            return float(mass.replace(",", ".")) < 0
        except ValueError:
            return False
    return False


def merge_autosaves(source_dir: Path, output_path: Path) -> Path:
    files = sorted(source_dir.glob("autosave_*.json"))
    if not files:
        raise FileNotFoundError(f"В папке нет autosave_*.json: {source_dir}")

    merged: "OrderedDict[str, dict]" = OrderedDict()
    newest_payload: dict | None = None
    for file in files:
        payload, records = load_records(file)
        newest_payload = payload
        for record in records:
            if should_skip_record(record):
                continue
            timestamp = str(record.get("timestamp") or "").strip()
            if not timestamp:
                continue
            merged[timestamp] = clean_record(record)

    result = {
        "metadata": {
            "version": "1.0",
            "merged_from": [file.name for file in files],
            "records_count": len(merged),
            "source_dir": str(source_dir),
        },
        "records": list(merged.values()),
        "plot_state": (newest_payload or {}).get("plot_state", {}),
        "config": (newest_payload or {}).get("config", {}),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Объединить autosave-файлы в одну очищенную сессию."
    )
    parser.add_argument(
        "--source-dir",
        default="sessions/autosave",
        help="Папка с autosave_*.json",
    )
    parser.add_argument(
        "--output",
        default="sessions/merged/merged_session_clean.json",
        help="Выходной JSON-файл сессии",
    )
    args = parser.parse_args()

    output = merge_autosaves(Path(args.source_dir), Path(args.output))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
