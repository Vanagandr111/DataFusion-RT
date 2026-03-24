from __future__ import annotations

import logging
from pathlib import Path


def setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    logging.captureWarnings(True)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("pymodbus").setLevel(logging.WARNING)

    logger = logging.getLogger("labforge")
    logger.info("Logging initialized. Log file: %s", log_path)
    return logger
