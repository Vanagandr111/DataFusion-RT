from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import sys
from tkinter import messagebox

from app.config import load_config, resolve_path
from app.crash_logging import bind_tk_crash_logging, install_crash_logging
from app.logger_setup import setup_logging
from app.models import AppConfig
from app.single_instance import acquire_single_instance
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
    crash_logger = install_crash_logging(resolve_path("logs"))
    args = build_parser().parse_args()

    if args.list_ports:
        print(format_port_listing(list_available_ports()))
        crash_logger.close()
        return 0

    acquired, _guard = acquire_single_instance("Global\\DataFusionRT_SingleInstance")
    if not acquired:
        try:
            messagebox.showerror(
                "DataFusion RT",
                "Программа уже запущена.\nЗакройте второй экземпляр перед новым запуском.",
            )
        except Exception:
            pass
        print("DataFusion RT already running.", file=sys.stderr)
        crash_logger.close()
        return 2

    config_path = resolve_path(args.config)

    try:
        config = load_config(config_path)
    except Exception as exc:
        print(f"Failed to load config: {exc}", file=sys.stderr)
        crash_logger.close()
        return 1

    logger = setup_logging(resolve_path(config.app.log_path), enable_file_logging=config.app.enable_file_logging)
    logger.info("DataFusion RT starting.")
    logger.info("Config path: %s", config_path)
    logger.info("Crash log path: %s", crash_logger.path)
    _log_port_diagnostics(logger, config)

    app = LabForgeApp(config=config, config_path=config_path, logger=logger)
    bind_tk_crash_logging(app, crash_logger)
    try:
        app.mainloop()
    finally:
        crash_logger.close()

    logger.info("DataFusion RT stopped.")
    return 0


def _log_port_diagnostics(logger, config: AppConfig) -> None:
    ports = list_available_ports()
    logger.info("%s", format_port_listing(ports))

    if config.app.test_mode:
        logger.info("Test mode is enabled in config.")

    if config.scale.enabled and not port_exists(config.scale.port):
        logger.warning("Configured scale port %s is not currently available for весы.", config.scale.port)
    if config.furnace.enabled and not port_exists(config.furnace.port):
        logger.warning("Configured furnace port %s is not currently available for печь.", config.furnace.port)


if __name__ == "__main__":
    raise SystemExit(main())
