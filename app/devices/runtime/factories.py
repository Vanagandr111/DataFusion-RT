from __future__ import annotations

import logging

from app.models import AppConfig
from app.devices.interfaces import FurnaceReaderProtocol, ScaleReaderProtocol
from app.devices.readers.furnace_reader import FurnaceReader
from app.devices.readers.passive_furnace_reader import PassiveFurnaceReader
from app.devices.readers.scale_reader import ScaleReader


def create_scale_reader(
    config: AppConfig,
    *,
    logger: logging.Logger,
) -> ScaleReaderProtocol:
    return ScaleReader(
        config.scale,
        test_mode=_scale_test_mode_enabled(config),
        logger=logger.getChild("scale"),
    )


def create_furnace_reader(
    config: AppConfig,
    *,
    logger: logging.Logger,
) -> FurnaceReaderProtocol:
    driver = (config.furnace.driver or "modbus").lower()
    if driver == "dk518":
        return PassiveFurnaceReader(
            config.furnace,
            test_mode=_furnace_test_mode_enabled(config),
            logger=logger.getChild("furnace"),
        )
    return FurnaceReader(
        config.furnace,
        test_mode=_furnace_test_mode_enabled(config),
        logger=logger.getChild("furnace"),
    )


def _scale_test_mode_enabled(config: AppConfig) -> bool:
    if not config.app.test_mode:
        return False
    return config.app.test_mode_scope in {"all", "scale"}


def _furnace_test_mode_enabled(config: AppConfig) -> bool:
    if not config.app.test_mode:
        return False
    return config.app.test_mode_scope in {"all", "furnace"}
