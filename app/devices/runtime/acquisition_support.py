from __future__ import annotations

import time

from app.devices.interfaces import FurnaceReaderProtocol
from app.devices.models.status import DeviceConnectionStatus, DeviceReadingStatus
from app.devices.models.runtime_state import AcquisitionRuntimeState
from app.models import AcquisitionSnapshot, AppConfig, MeasurementRecord


def build_acquisition_snapshot(
    config: AppConfig,
    runtime_state: AcquisitionRuntimeState,
    record: MeasurementRecord,
    *,
    scale_connected: bool,
    furnace_connected: bool,
) -> AcquisitionSnapshot:
    return AcquisitionSnapshot(
        record=record,
        scale_connected=scale_connected,
        furnace_connected=furnace_connected,
        scale_port=config.scale.port,
        furnace_port=config.furnace.port,
        test_mode=config.app.test_mode,
        sample_count=runtime_state.sample_count,
    )


def scale_poll_interval(config: AppConfig) -> float:
    if config.scale.p1_polling_enabled:
        return max(0.1, float(config.scale.p1_poll_interval_sec))
    return max(0.1, float(config.app.poll_interval_sec))


def furnace_poll_interval(config: AppConfig) -> float:
    return max(0.1, float(config.app.poll_interval_sec))


def next_wait_seconds(config: AppConfig, runtime_state: AcquisitionRuntimeState, cycle_started: float) -> float:
    targets = [
        target
        for target in (runtime_state.next_scale_poll_at, runtime_state.next_furnace_poll_at)
        if target > 0
    ]
    if not targets:
        return max(0.05, config.app.poll_interval_sec - (time.monotonic() - cycle_started))
    return max(0.05, min(targets) - time.monotonic())


def read_furnace_temperatures(reader: FurnaceReaderProtocol | None) -> tuple[float | None, float | None]:
    if reader is None:
        return None, None
    read_pair = getattr(reader, "read_temperatures", None)
    if callable(read_pair):
        return read_pair()
    return reader.read_pv(), reader.read_sv()


def build_scale_status(
    config: AppConfig,
    runtime_state: AcquisitionRuntimeState,
    *,
    connected: bool,
) -> DeviceReadingStatus:
    return DeviceReadingStatus(
        device_kind="scale",
        port=config.scale.port,
        connected=connected,
        last_timestamp=runtime_state.last_mass_timestamp,
        value=runtime_state.last_mass,
    )


def build_furnace_status(
    config: AppConfig,
    runtime_state: AcquisitionRuntimeState,
    *,
    connected: bool,
) -> DeviceReadingStatus:
    return DeviceReadingStatus(
        device_kind="furnace",
        port=config.furnace.port,
        connected=connected,
        last_timestamp=runtime_state.last_furnace_pv_timestamp or runtime_state.last_furnace_sv_timestamp,
        value=runtime_state.last_furnace_pv,
        secondary_value=runtime_state.last_furnace_sv,
    )


def build_scale_connection_status(config: AppConfig, *, connected: bool) -> DeviceConnectionStatus:
    return DeviceConnectionStatus(
        connected=connected,
        port=config.scale.port,
        driver="scale",
        test_mode=config.app.test_mode and config.app.test_mode_scope in {"all", "scale"},
    )


def build_furnace_connection_status(config: AppConfig, *, connected: bool) -> DeviceConnectionStatus:
    return DeviceConnectionStatus(
        connected=connected,
        port=config.furnace.port,
        driver=config.furnace.driver,
        test_mode=config.app.test_mode and config.app.test_mode_scope in {"all", "furnace"},
    )
