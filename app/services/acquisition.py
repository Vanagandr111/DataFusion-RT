from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime

from app.config import resolve_path
from app.models import AcquisitionSnapshot, AppConfig, MeasurementRecord
from app.services.data_logger import CSVDataLogger
from app.services.dk518_reader import DK518Reader
from app.services.furnace_reader import FurnaceReader
from app.services.passive_furnace_reader import PassiveFurnaceReader
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
        self._furnace_reader: FurnaceReader | DK518Reader | PassiveFurnaceReader | None = None
        self._last_mass: float | None = None
        self._last_furnace_pv: float | None = None
        self._last_furnace_sv: float | None = None
        self._last_mass_timestamp: str | None = None
        self._last_furnace_pv_timestamp: str | None = None
        self._last_furnace_sv_timestamp: str | None = None
        self._next_scale_poll_at = 0.0
        self._next_furnace_poll_at = 0.0

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
        self._sample_count = 0
        self._last_mass = None
        self._last_furnace_pv = None
        self._last_furnace_sv = None
        self._last_mass_timestamp = None
        self._last_furnace_pv_timestamp = None
        self._last_furnace_sv_timestamp = None
        self._next_scale_poll_at = 0.0
        self._next_furnace_poll_at = 0.0
        self._scale_reader = ScaleReader(
            self.config.scale,
            test_mode=self._scale_test_mode_enabled(),
            logger=self.logger.getChild("scale"),
        )
        self._furnace_reader = self._create_furnace_reader()
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
                if self._scale_reader and self.config.scale.enabled and now >= self._next_scale_poll_at:
                    mass = self._scale_reader.read_mass()
                    polled_any = True
                    self._next_scale_poll_at = now + self._scale_poll_interval()
                    if mass is not None:
                        self._last_mass = mass
                        self._last_mass_timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")

                if self._furnace_reader and self.config.furnace.enabled and now >= self._next_furnace_poll_at:
                    pv, sv = self._read_furnace_temperatures()
                    polled_any = True
                    self._next_furnace_poll_at = now + self._furnace_poll_interval()
                    if pv is not None:
                        self._last_furnace_pv = pv
                        self._last_furnace_pv_timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
                    if sv is not None:
                        self._last_furnace_sv = sv
                        self._last_furnace_sv_timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")

                if polled_any:
                    record = MeasurementRecord(
                        timestamp=datetime.now().astimezone().isoformat(timespec="milliseconds"),
                        mass=self._last_mass,
                        furnace_pv=self._last_furnace_pv,
                        furnace_sv=self._last_furnace_sv,
                        mass_timestamp=self._last_mass_timestamp,
                        furnace_pv_timestamp=self._last_furnace_pv_timestamp,
                        furnace_sv_timestamp=self._last_furnace_sv_timestamp,
                    )
                    self._sample_count += 1
                    self._csv_logger.append(record)
                    self._events.put(self._build_snapshot(record))
            except Exception:
                self.logger.exception("Unexpected error inside the acquisition loop.")

            remaining = self._next_wait_seconds(cycle_started)
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

    def _create_furnace_reader(self) -> FurnaceReader | DK518Reader | PassiveFurnaceReader:
        driver = (self.config.furnace.driver or "modbus").lower()
        if driver == "dk518":
            return PassiveFurnaceReader(
                self.config.furnace,
                test_mode=self._furnace_test_mode_enabled(),
                logger=self.logger.getChild("furnace"),
            )
        return FurnaceReader(
            self.config.furnace,
            test_mode=self._furnace_test_mode_enabled(),
            logger=self.logger.getChild("furnace"),
        )

    def _scale_test_mode_enabled(self) -> bool:
        if not self.config.app.test_mode:
            return False
        return self.config.app.test_mode_scope in {"all", "scale"}

    def _furnace_test_mode_enabled(self) -> bool:
        if not self.config.app.test_mode:
            return False
        return self.config.app.test_mode_scope in {"all", "furnace"}

    def _scale_poll_interval(self) -> float:
        if self.config.scale.p1_polling_enabled:
            return max(0.1, float(self.config.scale.p1_poll_interval_sec))
        return max(0.1, float(self.config.app.poll_interval_sec))

    def _furnace_poll_interval(self) -> float:
        return max(0.1, float(self.config.app.poll_interval_sec))

    def _next_wait_seconds(self, cycle_started: float) -> float:
        targets = [target for target in (self._next_scale_poll_at, self._next_furnace_poll_at) if target > 0]
        if not targets:
            return max(0.05, self.config.app.poll_interval_sec - (time.monotonic() - cycle_started))
        return max(0.05, min(targets) - time.monotonic())

    def _read_furnace_temperatures(self) -> tuple[float | None, float | None]:
        if self._furnace_reader is None:
            return None, None
        read_pair = getattr(self._furnace_reader, "read_temperatures", None)
        if callable(read_pair):
            return read_pair()
        return self._furnace_reader.read_pv(), self._furnace_reader.read_sv()
