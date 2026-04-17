from __future__ import annotations

import logging
import queue
import threading
import time

from app.config import resolve_path
from app.devices.interfaces import FurnaceReaderProtocol, ScaleReaderProtocol
from app.devices.models.runtime_state import AcquisitionRuntimeState
from app.devices.runtime.acquisition_support import (
    build_acquisition_snapshot,
    furnace_poll_interval,
    next_wait_seconds,
    read_furnace_temperatures,
    scale_poll_interval,
)
from app.devices.runtime.factories import create_furnace_reader, create_scale_reader
from app.models import AcquisitionSnapshot, AppConfig
from app.services.data_logger import CSVDataLogger


class AcquisitionController:
    def __init__(self, config: AppConfig, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self._events: queue.Queue[AcquisitionSnapshot] = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._sample_count = 0

        self._csv_logger = CSVDataLogger(
            resolve_path(self.config.app.csv_path),
            logger=self.logger.getChild("csv"),
        )
        self._scale_reader: ScaleReaderProtocol | None = None
        self._furnace_reader: FurnaceReaderProtocol | None = None
        self._runtime_state = AcquisitionRuntimeState()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def apply_runtime_settings(
        self,
        *,
        scale_port: str,
        furnace_port: str,
        test_mode: bool,
        test_mode_scope: str,
        scale_enabled: bool,
        furnace_enabled: bool,
    ) -> None:
        if self.running:
            raise RuntimeError("Cannot change runtime settings while acquisition is running.")

        self.config.scale.port = scale_port
        self.config.scale.enabled = scale_enabled
        self.config.furnace.port = furnace_port
        self.config.furnace.enabled = furnace_enabled
        self.config.app.test_mode = test_mode
        self.config.app.test_mode_scope = test_mode_scope
        self._csv_logger = CSVDataLogger(
            resolve_path(self.config.app.csv_path),
            logger=self.logger.getChild("csv"),
        )

    def start(self) -> bool:
        if self.running:
            self.logger.info("Acquisition is already running.")
            return False

        self._stop_event.clear()
        self._runtime_state.reset()
        self._scale_reader = create_scale_reader(self.config, logger=self.logger)
        self._furnace_reader = create_furnace_reader(self.config, logger=self.logger)
        self._thread = threading.Thread(
            target=self._run_loop,
            name="labforge-acquisition",
            daemon=True,
        )
        self._thread.start()
        self.logger.info("Acquisition started.")
        return True

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.config.app.poll_interval_sec + 1.0))
            self._thread = None

        self._close_readers()
        self.logger.info("Acquisition stopped.")

    def tare_scale(self) -> bool:
        if self._scale_reader is None:
            self.logger.warning("Scale tare requested before acquisition start.")
            return False
        return self._scale_reader.tare()

    def zero_scale(self) -> bool:
        if self._scale_reader is None:
            self.logger.warning("Scale zero requested before acquisition start.")
            return False
        return self._scale_reader.zero()

    def drain_snapshots(self) -> list[AcquisitionSnapshot]:
        snapshots: list[AcquisitionSnapshot] = []
        while True:
            try:
                snapshots.append(self._events.get_nowait())
            except queue.Empty:
                return snapshots

    def close(self) -> None:
        if self.running:
            self.stop()
        else:
            self._close_readers()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            cycle_started = time.monotonic()
            polled_any = False
            try:
                now = time.monotonic()
                if self._scale_reader and self.config.scale.enabled and now >= self._runtime_state.next_scale_poll_at:
                    mass = self._scale_reader.read_mass()
                    polled_any = True
                    self._runtime_state.next_scale_poll_at = now + scale_poll_interval(self.config)
                    self._runtime_state.record_mass(mass)

                if self._furnace_reader and self.config.furnace.enabled and now >= self._runtime_state.next_furnace_poll_at:
                    pv, sv = read_furnace_temperatures(self._furnace_reader)
                    polled_any = True
                    self._runtime_state.next_furnace_poll_at = now + furnace_poll_interval(self.config)
                    self._runtime_state.record_furnace(pv, sv)

                if polled_any:
                    record = self._runtime_state.build_record()
                    self._csv_logger.append(record)
                    self._events.put(self._build_snapshot(record))
            except Exception:
                self.logger.exception("Unexpected error inside the acquisition loop.")

            remaining = next_wait_seconds(self.config, self._runtime_state, cycle_started)
            if remaining > 0:
                self._stop_event.wait(remaining)

    def _build_snapshot(self, record) -> AcquisitionSnapshot:
        return build_acquisition_snapshot(
            self.config,
            self._runtime_state,
            record,
            scale_connected=self._scale_reader.connected if self._scale_reader else False,
            furnace_connected=self._furnace_reader.connected if self._furnace_reader else False,
        )

    def _close_readers(self) -> None:
        if self._scale_reader is not None:
            self._scale_reader.close()
            self._scale_reader = None
        if self._furnace_reader is not None:
            self._furnace_reader.close()
            self._furnace_reader = None

