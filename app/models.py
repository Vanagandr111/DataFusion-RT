from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ScaleConfig:
    enabled: bool
    port: str
    baudrate: int
    timeout: float
    mode: str
    request_command: str
    p1_polling_enabled: bool = False
    p1_poll_interval_sec: float = 0.1


@dataclass(slots=True)
class FurnaceConfig:
    enabled: bool
    port: str
    baudrate: int
    bytesize: int
    parity: str
    stopbits: float
    timeout: float
    slave_id: int
    register_pv: int
    register_sv: int
    scale_factor: float
    driver: str = "generic_modbus"
    access_mode: str = "read_only"
    read_groups: list[dict[str, object]] = field(default_factory=list)
    window_enabled: bool = False
    window_period_ms: int = 1000
    window_open_ms: int = 120
    window_offset_ms: int = 0
    experimental_write_enabled: bool = False
    input_type_code: int = 0
    input_type_name: str = "K"
    high_limit: float = 1200.0
    high_alarm: float = 999.9
    low_alarm: float = 999.9
    pid_p: float = 10.0
    pid_t: float = 8.0
    ctrl_mode: int = 3
    output_high_limit: float = 100.0
    display_decimals: int = 2
    sensor_correction: float = 0.0
    opt_code: int = 8
    run_code: int = 27
    alarm_output_code: int = 3333
    m5_value: float = 420.0


@dataclass(slots=True)
class ApplicationConfig:
    poll_interval_sec: float
    csv_path: str
    log_path: str
    max_points_on_plot: int
    auto_detect_ports: bool = True
    test_mode: bool = False
    test_mode_scope: str = "all"
    autosave_settings: bool = False
    enable_file_logging: bool = False
    theme: str = "dark"
    start_maximized: bool = False
    fullscreen: bool = False
    font_scale: float = 1.0
    default_export_format: str = "csv"
    plot_styles: dict[str, dict[str, object]] = field(default_factory=dict)
    plot_autoscale_enabled: bool = True
    plot_manual_x_seconds: float = 600.0
    plot_manual_y_span: float = 250.0
    plot_y_headroom: float = 50.0


@dataclass(slots=True)
class AppConfig:
    scale: ScaleConfig
    furnace: FurnaceConfig
    app: ApplicationConfig


@dataclass(slots=True)
class MeasurementRecord:
    timestamp: str
    mass: float | None
    furnace_pv: float | None
    furnace_sv: float | None
    mass_timestamp: str | None = None
    furnace_pv_timestamp: str | None = None
    furnace_sv_timestamp: str | None = None

    def as_dict(self) -> dict[str, float | str | None]:
        return {
            "timestamp": self.timestamp,
            "mass": self.mass,
            "furnace_pv": self.furnace_pv,
            "furnace_sv": self.furnace_sv,
            "mass_timestamp": self.mass_timestamp,
            "furnace_pv_timestamp": self.furnace_pv_timestamp,
            "furnace_sv_timestamp": self.furnace_sv_timestamp,
        }


@dataclass(slots=True)
class AcquisitionSnapshot:
    record: MeasurementRecord
    scale_connected: bool
    furnace_connected: bool
    scale_port: str
    furnace_port: str
    test_mode: bool
    sample_count: int


@dataclass(slots=True)
class PortInfo:
    device: str
    description: str
    hwid: str
