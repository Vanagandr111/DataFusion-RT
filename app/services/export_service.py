from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
from app.services.excel_support import write_excel_frame


class MeasurementExportService:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger(__name__)

    def export(self, source_csv: Path, destination: Path) -> tuple[bool, str]:
        try:
            frame = self._load_frame(source_csv)
        except FileNotFoundError:
            return False, f"Файл измерений не найден: {source_csv}"
        except ValueError as exc:
            return False, str(exc)
        except Exception as exc:
            self.logger.error("Не удалось подготовить данные для экспорта: %s", exc)
            return False, f"Не удалось подготовить данные для экспорта: {exc}"

        destination.parent.mkdir(parents=True, exist_ok=True)
        extension = destination.suffix.lower()

        try:
            if extension == ".csv":
                frame.to_csv(destination, index=False, encoding="utf-8-sig")
            elif extension == ".xlsx":
                write_excel_frame(frame, destination)
            elif extension == ".xml":
                frame.to_xml(destination, index=False, root_name="measurements", row_name="measurement")
            else:
                return False, f"Неподдерживаемый формат экспорта: {destination.suffix}"
        except ImportError as exc:
            self.logger.warning("Не хватает зависимости для экспорта %s: %s", destination.suffix, exc)
            return False, f"Для экспорта в {destination.suffix} не хватает зависимости: {exc}"
        except Exception as exc:
            self.logger.error("Ошибка экспорта в %s: %s", destination, exc)
            return False, f"Ошибка экспорта: {exc}"

        self.logger.info("Measurements exported to %s", destination)
        return True, f"Данные экспортированы: {destination}"

    def export_frame(self, frame: pd.DataFrame, destination: Path) -> tuple[bool, str]:
        if frame.empty:
            return False, "Нет данных для экспорта."

        destination.parent.mkdir(parents=True, exist_ok=True)
        extension = destination.suffix.lower()

        try:
            if extension == ".csv":
                frame.to_csv(destination, index=False, encoding="utf-8-sig")
            elif extension == ".xlsx":
                write_excel_frame(frame, destination)
            elif extension == ".xml":
                frame.to_xml(destination, index=False, root_name="measurements", row_name="measurement")
            else:
                return False, f"Неподдерживаемый формат экспорта: {destination.suffix}"
        except ImportError as exc:
            self.logger.warning("Не хватает зависимости для экспорта %s: %s", destination.suffix, exc)
            return False, f"Для экспорта в {destination.suffix} не хватает зависимости: {exc}"
        except Exception as exc:
            self.logger.error("Ошибка экспорта в %s: %s", destination, exc)
            return False, f"Ошибка экспорта: {exc}"

        self.logger.info("Measurements exported to %s", destination)
        return True, f"Данные экспортированы: {destination}"

    def _load_frame(self, source_csv: Path) -> pd.DataFrame:
        if not source_csv.exists():
            raise FileNotFoundError(source_csv)

        frame = pd.read_csv(source_csv)
        if frame.empty:
            raise ValueError("Нет данных для экспорта.")

        normalized = pd.DataFrame()
        normalized["№"] = range(1, len(frame) + 1)
        normalized["Время"] = frame.get("timestamp")
        normalized["Масса, г"] = frame.get("mass")
        normalized["Температура камеры PV, °C"] = frame.get("furnace_pv")
        normalized["Температура термопары SV, °C"] = frame.get("furnace_sv")
        return normalized
