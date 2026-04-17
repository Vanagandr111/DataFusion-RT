from __future__ import annotations

from datetime import datetime

from app.compat import dataclass
from app.models import MeasurementRecord


def iso_now_ms() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


@dataclass(slots=True)
class AcquisitionRuntimeState:
    sample_count: int = 0
    last_mass: float | None = None
    last_furnace_pv: float | None = None
    last_furnace_sv: float | None = None
    last_mass_timestamp: str | None = None
    last_furnace_pv_timestamp: str | None = None
    last_furnace_sv_timestamp: str | None = None
    next_scale_poll_at: float = 0.0
    next_furnace_poll_at: float = 0.0

    def reset(self) -> None:
        self.sample_count = 0
        self.last_mass = None
        self.last_furnace_pv = None
        self.last_furnace_sv = None
        self.last_mass_timestamp = None
        self.last_furnace_pv_timestamp = None
        self.last_furnace_sv_timestamp = None
        self.next_scale_poll_at = 0.0
        self.next_furnace_poll_at = 0.0

    def record_mass(self, value: float | None) -> None:
        if value is None:
            return
        self.last_mass = value
        self.last_mass_timestamp = iso_now_ms()

    def record_furnace(self, pv: float | None, sv: float | None) -> None:
        if pv is not None:
            self.last_furnace_pv = pv
            self.last_furnace_pv_timestamp = iso_now_ms()
        if sv is not None:
            self.last_furnace_sv = sv
            self.last_furnace_sv_timestamp = iso_now_ms()

    def build_record(self) -> MeasurementRecord:
        self.sample_count += 1
        return MeasurementRecord(
            timestamp=iso_now_ms(),
            mass=self.last_mass,
            furnace_pv=self.last_furnace_pv,
            furnace_sv=self.last_furnace_sv,
            mass_timestamp=self.last_mass_timestamp,
            furnace_pv_timestamp=self.last_furnace_pv_timestamp,
            furnace_sv_timestamp=self.last_furnace_sv_timestamp,
        )
