from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ScaleConfig:
    enabled: bool
    port: str
    baudrate: int
    timeout: float
    mode: str
    request_command: str


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


@dataclass(slots=True)
class ApplicationConfig:
    poll_interval_sec: float
    csv_path: str
    log_path: str
    max_points_on_plot: int
    test_mode: bool = False
    autosave_settings: bool = False
    enable_file_logging: bool = False
    theme: str = "dark"
    start_maximized: bool = False
    fullscreen: bool = False
    font_scale: float = 1.0
    default_export_format: str = "csv"


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

    def as_dict(self) -> dict[str, float | str | None]:
        return {
            "timestamp": self.timestamp,
            "mass": self.mass,
            "furnace_pv": self.furnace_pv,
            "furnace_sv": self.furnace_sv,
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
