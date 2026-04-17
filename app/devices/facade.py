from __future__ import annotations

import logging

from app.devices.models.runtime_state import AcquisitionRuntimeState
from app.devices.models.status import (
    DeviceConnectionStatus,
    DeviceReadingStatus,
    ProbeResult,
)
from app.devices.runtime.acquisition_support import (
    build_furnace_connection_status,
    build_furnace_status,
    build_scale_connection_status,
    build_scale_status,
    read_furnace_temperatures,
)
from app.models import AppConfig


class DeviceFacade:
    def __init__(self, config: AppConfig, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.runtime_state = AcquisitionRuntimeState()

    def create_scale_reader(self):
        from app.devices.runtime.factories import create_scale_reader

        return create_scale_reader(self.config, logger=self.logger)

    def create_furnace_reader(self):
        from app.devices.runtime.factories import create_furnace_reader

        return create_furnace_reader(self.config, logger=self.logger)

    def probe_scale(self) -> ProbeResult:
        from app.devices.probe.device_probe import probe_scale

        return probe_scale(
            self.config.scale,
            test_mode=self.config.app.test_mode and self.config.app.test_mode_scope in {"all", "scale"},
            logger=self.logger.getChild("probe.scale"),
        )

    def probe_furnace(self) -> ProbeResult:
        from app.devices.probe.device_probe import probe_furnace

        return probe_furnace(
            self.config.furnace,
            test_mode=self.config.app.test_mode and self.config.app.test_mode_scope in {"all", "furnace"},
            logger=self.logger.getChild("probe.furnace"),
        )

    def sample_scale(self, reader) -> DeviceReadingStatus:
        self.runtime_state.record_mass(reader.read_mass())
        return build_scale_status(self.config, self.runtime_state, connected=reader.connected)

    def sample_furnace(self, reader) -> DeviceReadingStatus:
        pv, sv = read_furnace_temperatures(reader)
        self.runtime_state.record_furnace(pv, sv)
        return build_furnace_status(self.config, self.runtime_state, connected=reader.connected)

    def scale_connection_status(self, reader=None) -> DeviceConnectionStatus:
        return build_scale_connection_status(
            self.config,
            connected=reader.connected if reader is not None else False,
        )

    def furnace_connection_status(self, reader=None) -> DeviceConnectionStatus:
        return build_furnace_connection_status(
            self.config,
            connected=reader.connected if reader is not None else False,
        )
