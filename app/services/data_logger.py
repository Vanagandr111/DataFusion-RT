from __future__ import annotations

import csv
import logging
from pathlib import Path

from app.models import MeasurementRecord


class CSVDataLogger:
    def __init__(self, csv_path: Path, logger: logging.Logger | None = None) -> None:
        self.csv_path = csv_path
        self.logger = logger or logging.getLogger(__name__)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._fieldnames = ("timestamp", "mass", "furnace_pv", "furnace_sv")
        self._header_written = self.csv_path.exists() and self.csv_path.stat().st_size > 0

    def append(self, record: MeasurementRecord) -> None:
        try:
            with self.csv_path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=self._fieldnames)
                if not self._header_written:
                    writer.writeheader()
                    self._header_written = True
                writer.writerow(record.as_dict())
        except Exception:
            self.logger.exception("Failed to append measurement row to CSV: %s", self.csv_path)
