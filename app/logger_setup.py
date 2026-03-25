from __future__ import annotations

import logging
from pathlib import Path

APP_LOGGER_NAME = "DataFusionRT"


def _build_formatter() -> logging.Formatter:
    return logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _build_file_handler(log_path: Path) -> logging.FileHandler:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(_build_formatter())
    file_handler._datafusion_file_handler = True  # type: ignore[attr-defined]
    return file_handler


def setup_logging(log_path: Path, *, enable_file_logging: bool) -> logging.Logger:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(_build_formatter())

    root_logger.addHandler(console_handler)
    if enable_file_logging:
        root_logger.addHandler(_build_file_handler(log_path))

    logging.captureWarnings(True)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("pymodbus").setLevel(logging.WARNING)

    logger = logging.getLogger(APP_LOGGER_NAME)
    if enable_file_logging:
        logger.info("Логирование инициализировано. Файл журнала: %s", log_path)
    else:
        logger.info("Логирование инициализировано. Запись в файл отключена.")
    return logger


def reconfigure_file_logging(log_path: Path, *, enable_file_logging: bool) -> None:
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if getattr(handler, "_datafusion_file_handler", False):
            root_logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
    if enable_file_logging:
        root_logger.addHandler(_build_file_handler(log_path))
