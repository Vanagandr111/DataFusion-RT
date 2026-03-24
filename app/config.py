from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

from app.models import AppConfig, ApplicationConfig, FurnaceConfig, ScaleConfig


def runtime_base_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def default_config_path() -> Path:
    return runtime_base_path() / "config" / "config.yaml"


def resolve_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return runtime_base_path() / path


def load_config(config_path: str | Path | None = None) -> AppConfig:
    path = Path(config_path) if config_path else default_config_path()
    if not path.is_absolute():
        path = runtime_base_path() / path

    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        raw_config = yaml.safe_load(handle) or {}

    if not isinstance(raw_config, dict):
        raise ValueError("Top-level YAML config must be a mapping.")

    scale_raw = _require_section(raw_config, "scale")
    furnace_raw = _require_section(raw_config, "furnace")
    app_raw = _require_section(raw_config, "app")

    return AppConfig(
        scale=ScaleConfig(
            enabled=bool(scale_raw.get("enabled", True)),
            port=str(scale_raw.get("port", "COM3")),
            baudrate=int(scale_raw.get("baudrate", 4800)),
            timeout=float(scale_raw.get("timeout", 1.0)),
            mode=str(scale_raw.get("mode", "auto")).strip().lower(),
            request_command=str(scale_raw.get("request_command", "P\r\n")),
        ),
        furnace=FurnaceConfig(
            enabled=bool(furnace_raw.get("enabled", True)),
            port=str(furnace_raw.get("port", "COM4")),
            baudrate=int(furnace_raw.get("baudrate", 9600)),
            bytesize=int(furnace_raw.get("bytesize", 8)),
            parity=str(furnace_raw.get("parity", "N")).strip().upper()[:1] or "N",
            stopbits=float(furnace_raw.get("stopbits", 1)),
            timeout=float(furnace_raw.get("timeout", 1.0)),
            slave_id=int(furnace_raw.get("slave_id", 1)),
            register_pv=int(furnace_raw.get("register_pv", 0)),
            register_sv=int(furnace_raw.get("register_sv", 1)),
            scale_factor=float(furnace_raw.get("scale_factor", 0.1)),
        ),
        app=ApplicationConfig(
            poll_interval_sec=float(app_raw.get("poll_interval_sec", 1.0)),
            csv_path=str(app_raw.get("csv_path", "data/measurements.csv")),
            log_path=str(app_raw.get("log_path", "logs/app.log")),
            max_points_on_plot=int(app_raw.get("max_points_on_plot", 500)),
            test_mode=bool(app_raw.get("test_mode", False)),
            theme=str(app_raw.get("theme", "dark")).strip().lower() or "dark",
            start_maximized=bool(app_raw.get("start_maximized", True)),
            fullscreen=bool(app_raw.get("fullscreen", False)),
            default_export_format=str(app_raw.get("default_export_format", "csv")).strip().lower() or "csv",
        ),
    )


def save_config(config: AppConfig, config_path: str | Path | None = None) -> Path:
    path = Path(config_path) if config_path else default_config_path()
    if not path.is_absolute():
        path = runtime_base_path() / path

    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(
            config_to_dict(config),
            handle,
            allow_unicode=True,
            sort_keys=False,
        )

    return path


def config_to_dict(config: AppConfig) -> dict[str, dict[str, object]]:
    return {
        "scale": {
            "enabled": config.scale.enabled,
            "port": config.scale.port,
            "baudrate": config.scale.baudrate,
            "timeout": config.scale.timeout,
            "mode": config.scale.mode,
            "request_command": config.scale.request_command,
        },
        "furnace": {
            "enabled": config.furnace.enabled,
            "port": config.furnace.port,
            "baudrate": config.furnace.baudrate,
            "bytesize": config.furnace.bytesize,
            "parity": config.furnace.parity,
            "stopbits": config.furnace.stopbits,
            "timeout": config.furnace.timeout,
            "slave_id": config.furnace.slave_id,
            "register_pv": config.furnace.register_pv,
            "register_sv": config.furnace.register_sv,
            "scale_factor": config.furnace.scale_factor,
        },
        "app": {
            "poll_interval_sec": config.app.poll_interval_sec,
            "csv_path": config.app.csv_path,
            "log_path": config.app.log_path,
            "max_points_on_plot": config.app.max_points_on_plot,
            "test_mode": config.app.test_mode,
            "theme": config.app.theme,
            "start_maximized": config.app.start_maximized,
            "fullscreen": config.app.fullscreen,
            "default_export_format": config.app.default_export_format,
        },
    }


def _require_section(data: dict[str, Any], name: str) -> dict[str, Any]:
    section = data.get(name)
    if not isinstance(section, dict):
        raise ValueError(f"Config section '{name}' is missing or invalid.")
    return section
