from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from app.models import MeasurementRecord


class CSVDataLogger:
    def __init__(self, csv_path: Path, logger: logging.Logger | None = None) -> None:
        self.csv_path = csv_path
        self.logger = logger or logging.getLogger(__name__)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: MeasurementRecord) -> None:
        frame = pd.DataFrame([record.as_dict()])
        write_header = not self.csv_path.exists()

        try:
            frame.to_csv(
                self.csv_path,
                mode="a",
                header=write_header,
                index=False,
                encoding="utf-8",
            )
        except Exception:
            self.logger.exception("Failed to append measurement row to CSV: %s", self.csv_path)
