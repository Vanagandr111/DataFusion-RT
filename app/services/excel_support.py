from __future__ import annotations

from pathlib import Path

import pandas as pd


def read_excel_frame(path: Path) -> pd.DataFrame:
    try:
        import openpyxl  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "Не найден модуль openpyxl. Установите зависимость для импорта Excel."
        ) from exc
    return pd.read_excel(path, engine="openpyxl")


def write_excel_frame(frame: pd.DataFrame, destination: Path) -> None:
    try:
        import openpyxl  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "Не найден модуль openpyxl. Установите зависимость для экспорта Excel."
        ) from exc
    frame.to_excel(destination, index=False, engine="openpyxl")
