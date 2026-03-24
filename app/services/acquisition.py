from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime

from app.config import resolve_path
from app.models import AcquisitionSnapshot, AppConfig, MeasurementRecord
from app.services.data_logger import CSVDataLogger
from app.services.furnace_reader import FurnaceReader
from app.services.scale_reader import ScaleReader


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
        self._scale_reader: ScaleReader | None = None
        self._furnace_reader: FurnaceReader | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def apply_runtime_settings(
        self,
        *,
        scale_port: str,
        furnace_port: str,
        test_mode: bool,
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
        self._csv_logger = CSVDataLogger(
            resolve_path(self.config.app.csv_path),
            logger=self.logger.getChild("csv"),
        )

    def start(self) -> bool:
        if self.running:
            self.logger.info("Acquisition is already running.")
            return False

        self._stop_event.clear()
        self._sample_count = 0
        self._scale_reader = ScaleReader(
            self.config.scale,
            test_mode=self.config.app.test_mode,
            logger=self.logger.getChild("scale"),
        )
        self._furnace_reader = FurnaceReader(
            self.config.furnace,
            test_mode=self.config.app.test_mode,
            logger=self.logger.getChild("furnace"),
        )
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
            try:
                record = MeasurementRecord(
                    timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
                    mass=self._scale_reader.read_mass() if self._scale_reader and self.config.scale.enabled else None,
                    furnace_pv=(
                        self._furnace_reader.read_pv()
                        if self._furnace_reader and self.config.furnace.enabled
                        else None
                    ),
                    furnace_sv=(
                        self._furnace_reader.read_sv()
                        if self._furnace_reader and self.config.furnace.enabled
                        else None
                    ),
                )
                self._sample_count += 1
                self._csv_logger.append(record)
                self._events.put(self._build_snapshot(record))
            except Exception:
                self.logger.exception("Unexpected error inside the acquisition loop.")

            remaining = self.config.app.poll_interval_sec - (time.monotonic() - cycle_started)
            if remaining > 0:
                self._stop_event.wait(remaining)

    def _build_snapshot(self, record: MeasurementRecord) -> AcquisitionSnapshot:
        return AcquisitionSnapshot(
            record=record,
            scale_connected=self._scale_reader.connected if self._scale_reader else False,
            furnace_connected=self._furnace_reader.connected if self._furnace_reader else False,
            scale_port=self.config.scale.port,
            furnace_port=self.config.furnace.port,
            test_mode=self.config.app.test_mode,
            sample_count=self._sample_count,
        )

    def _close_readers(self) -> None:
        if self._scale_reader is not None:
            self._scale_reader.close()
            self._scale_reader = None
        if self._furnace_reader is not None:
            self._furnace_reader.close()
            self._furnace_reader = None
