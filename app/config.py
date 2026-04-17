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

    furnace_driver = str(furnace_raw.get("driver", "modbus")).strip().lower() or "modbus"
    if furnace_driver == "generic_modbus":
        furnace_driver = "modbus"
    read_groups = list(furnace_raw.get("read_groups", []) or [])
    if furnace_driver == "dk518" and not read_groups:
        read_groups = _default_dk518_read_groups()

    return AppConfig(
        scale=ScaleConfig(
            enabled=bool(scale_raw.get("enabled", True)),
            port=str(scale_raw.get("port", "COM3")),
            baudrate=int(scale_raw.get("baudrate", 9600)),
            timeout=float(scale_raw.get("timeout", 1.0)),
            mode=str(scale_raw.get("mode", "continuous")).strip().lower(),
            request_command=str(scale_raw.get("request_command", "P\r\n")),
            p1_polling_enabled=bool(scale_raw.get("p1_polling_enabled", False)),
            p1_poll_interval_sec=float(scale_raw.get("p1_poll_interval_sec", 0.1)),
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
            register_pv=int(furnace_raw.get("register_pv", 90)),
            register_sv=int(furnace_raw.get("register_sv", 91)),
            scale_factor=float(furnace_raw.get("scale_factor", 0.1)),
            register_mode_pv=int(furnace_raw.get("register_mode_pv", 4 if furnace_driver == "dk518" else 3)),
            register_mode_sv=int(furnace_raw.get("register_mode_sv", 4 if furnace_driver == "dk518" else 3)),
            driver=furnace_driver,
            access_mode=str(furnace_raw.get("access_mode", "read_only" if furnace_driver == "dk518" else "active_modbus")).strip().lower() or ("read_only" if furnace_driver == "dk518" else "active_modbus"),
            read_groups=read_groups,
            window_enabled=False,
            window_period_ms=int(furnace_raw.get("window_period_ms", 1000)),
            window_open_ms=int(furnace_raw.get("window_open_ms", 120)),
            window_offset_ms=int(furnace_raw.get("window_offset_ms", 0)),
            experimental_write_enabled=False,
            input_type_code=int(furnace_raw.get("input_type_code", 0)),
            input_type_name=str(furnace_raw.get("input_type_name", "K")),
            high_limit=float(furnace_raw.get("high_limit", 1200.0)),
            high_alarm=float(furnace_raw.get("high_alarm", 999.9)),
            low_alarm=float(furnace_raw.get("low_alarm", 999.9)),
            pid_p=float(furnace_raw.get("pid_p", 10.0)),
            pid_t=float(furnace_raw.get("pid_t", 8.0)),
            ctrl_mode=int(furnace_raw.get("ctrl_mode", 3)),
            output_high_limit=float(furnace_raw.get("output_high_limit", 100.0)),
            display_decimals=int(furnace_raw.get("display_decimals", 2)),
            sensor_correction=float(furnace_raw.get("sensor_correction", 0.0)),
            opt_code=int(furnace_raw.get("opt_code", 8)),
            run_code=int(furnace_raw.get("run_code", 27)),
            alarm_output_code=int(furnace_raw.get("alarm_output_code", 3333)),
            m5_value=float(furnace_raw.get("m5_value", 420.0)),
        ),
        app=ApplicationConfig(
            poll_interval_sec=float(app_raw.get("poll_interval_sec", 1.0)),
            csv_path=str(app_raw.get("csv_path", "data/measurements.csv")),
            log_path=str(app_raw.get("log_path", "logs/app.log")),
            max_points_on_plot=int(app_raw.get("max_points_on_plot", 500)),
            auto_detect_ports=bool(app_raw.get("auto_detect_ports", True)),
            test_mode=bool(app_raw.get("test_mode", False)),
            test_mode_scope=str(app_raw.get("test_mode_scope", "all")).strip().lower() or "all",
            autosave_settings=bool(app_raw.get("autosave_settings", False)),
            enable_file_logging=bool(app_raw.get("enable_file_logging", False)),
            theme=str(app_raw.get("theme", "dark")).strip().lower() or "dark",
            start_maximized=bool(app_raw.get("start_maximized", False)),
            fullscreen=bool(app_raw.get("fullscreen", False)),
            font_scale=float(app_raw.get("font_scale", 1.0)),
            default_export_format=str(app_raw.get("default_export_format", "csv")).strip().lower() or "csv",
            plot_styles=dict(app_raw.get("plot_styles", {}) or {}),
            plot_autoscale_enabled=bool(app_raw.get("plot_autoscale_enabled", True)),
            plot_manual_x_seconds=float(app_raw.get("plot_manual_x_seconds", 600.0)),
            plot_manual_y_span=float(app_raw.get("plot_manual_y_span", 250.0)),
            plot_y_headroom=float(app_raw.get("plot_y_headroom", 50.0)),
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
            "p1_polling_enabled": config.scale.p1_polling_enabled,
            "p1_poll_interval_sec": config.scale.p1_poll_interval_sec,
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
            "register_mode_pv": config.furnace.register_mode_pv,
            "register_mode_sv": config.furnace.register_mode_sv,
            "driver": config.furnace.driver,
            "access_mode": config.furnace.access_mode,
            "read_groups": config.furnace.read_groups,
            "window_enabled": False,
            "window_period_ms": config.furnace.window_period_ms,
            "window_open_ms": config.furnace.window_open_ms,
            "window_offset_ms": config.furnace.window_offset_ms,
            "experimental_write_enabled": False,
            "input_type_code": config.furnace.input_type_code,
            "input_type_name": config.furnace.input_type_name,
            "high_limit": config.furnace.high_limit,
            "high_alarm": config.furnace.high_alarm,
            "low_alarm": config.furnace.low_alarm,
            "pid_p": config.furnace.pid_p,
            "pid_t": config.furnace.pid_t,
            "ctrl_mode": config.furnace.ctrl_mode,
            "output_high_limit": config.furnace.output_high_limit,
            "display_decimals": config.furnace.display_decimals,
            "sensor_correction": config.furnace.sensor_correction,
            "opt_code": config.furnace.opt_code,
            "run_code": config.furnace.run_code,
            "alarm_output_code": config.furnace.alarm_output_code,
            "m5_value": config.furnace.m5_value,
        },
        "app": {
            "poll_interval_sec": config.app.poll_interval_sec,
            "csv_path": config.app.csv_path,
            "log_path": config.app.log_path,
            "max_points_on_plot": config.app.max_points_on_plot,
            "auto_detect_ports": config.app.auto_detect_ports,
            "test_mode": config.app.test_mode,
            "test_mode_scope": config.app.test_mode_scope,
            "autosave_settings": config.app.autosave_settings,
            "enable_file_logging": config.app.enable_file_logging,
            "theme": config.app.theme,
            "start_maximized": config.app.start_maximized,
            "fullscreen": config.app.fullscreen,
            "font_scale": config.app.font_scale,
            "default_export_format": config.app.default_export_format,
            "plot_styles": config.app.plot_styles,
            "plot_autoscale_enabled": config.app.plot_autoscale_enabled,
            "plot_manual_x_seconds": config.app.plot_manual_x_seconds,
            "plot_manual_y_span": config.app.plot_manual_y_span,
            "plot_y_headroom": config.app.plot_y_headroom,
        },
    }


def _default_dk518_read_groups() -> list[dict[str, object]]:
    return [
        {
            "name": "input_temperature_block",
            "function": 4,
            "address": 90,
            "count": 2,
            "scale": 0.1,
            "pv_index": 0,
            "sv_index": 1,
        },
        {"name": "hold_0x0015", "function": 3, "address": 21, "count": 3, "scale": 1.0},
        {"name": "hold_0x0056", "function": 3, "address": 86, "count": 3, "scale": 1.0},
        {"name": "hold_0x0006", "function": 3, "address": 6, "count": 3, "scale": 1.0},
    ]


def _require_section(data: dict[str, Any], name: str) -> dict[str, Any]:
    section = data.get(name)
    if not isinstance(section, dict):
        raise ValueError(f"Config section '{name}' is missing or invalid.")
    return section
