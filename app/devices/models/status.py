from __future__ import annotations

from app.compat import dataclass


@dataclass(slots=True)
class ProbeResult:
    ok: bool
    message: str
    port: str
    device_kind: str
    details: str | None = None

    def as_tuple(self) -> tuple[bool, str]:
        return self.ok, self.message


@dataclass(slots=True)
class DeviceConnectionStatus:
    connected: bool
    port: str
    driver: str
    test_mode: bool


@dataclass(slots=True)
class DeviceReadingStatus:
    device_kind: str
    port: str
    connected: bool
    last_timestamp: str | None
    value: float | None = None
    secondary_value: float | None = None
