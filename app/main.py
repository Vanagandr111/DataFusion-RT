from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import sys

from app.config import load_config, resolve_path
from app.logger_setup import setup_logging
from app.models import AppConfig
from app.ui import LabForgeApp
from app.utils.serial_tools import format_port_listing, list_available_ports, port_exists


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DataFusion RT data acquisition for scale and furnace.")
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to YAML config file. Relative paths are resolved from project root or EXE folder.",
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="Print currently available COM ports and exit.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.list_ports:
        print(format_port_listing(list_available_ports()))
        return 0

    config_path = resolve_path(args.config)

    try:
        config = load_config(config_path)
    except Exception as exc:
        print(f"Failed to load config: {exc}", file=sys.stderr)
        return 1

    logger = setup_logging(resolve_path(config.app.log_path))
    logger.info("DataFusion RT starting.")
    logger.info("Config path: %s", config_path)
    _log_port_diagnostics(logger, config)

    app = LabForgeApp(config=config, config_path=config_path, logger=logger)
    app.mainloop()

    logger.info("DataFusion RT stopped.")
    return 0


def _log_port_diagnostics(logger, config: AppConfig) -> None:
    ports = list_available_ports()
    logger.info("%s", format_port_listing(ports))

    if config.app.test_mode:
        logger.info("Test mode is enabled in config.")

    if config.scale.enabled and not port_exists(config.scale.port):
        logger.warning("Configured scale port %s is not currently available.", config.scale.port)
    if config.furnace.enabled and not port_exists(config.furnace.port):
        logger.warning("Configured furnace port %s is not currently available.", config.furnace.port)


if __name__ == "__main__":
    raise SystemExit(main())
