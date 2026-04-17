from __future__ import annotations

import faulthandler
import platform
import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TextIO


@dataclass
class CrashLogger:
    path: Path
    handle: TextIO

    def write_banner(self) -> None:
        self.handle.write(
            "\n"
            + "=" * 72
            + "\n"
            + f"Crash session started: {datetime.now().isoformat(timespec='seconds')}\n"
            + f"Python: {sys.version}\n"
            + f"Platform: {platform.platform()}\n"
            + f"Executable: {sys.executable}\n"
            + f"Frozen: {getattr(sys, 'frozen', False)}\n"
            + "=" * 72
            + "\n"
        )
        self.handle.flush()

    def write_exception(
        self,
        title: str,
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        self.handle.write(
            "\n"
            + "-" * 72
            + "\n"
            + f"{title}: {datetime.now().isoformat(timespec='seconds')}\n"
        )
        traceback.print_exception(exc_type, exc_value, exc_traceback, file=self.handle)
        self.handle.write("-" * 72 + "\n")
        self.handle.flush()

    def close(self) -> None:
        try:
            faulthandler.disable()
        except Exception:
            pass
        try:
            self.handle.flush()
        except Exception:
            pass
        try:
            self.handle.close()
        except Exception:
            pass


def install_crash_logging(log_dir: Path) -> CrashLogger:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"crash_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
    handle = path.open("a", encoding="utf-8")
    crash_logger = CrashLogger(path=path, handle=handle)
    crash_logger.write_banner()
    try:
        faulthandler.enable(handle, all_threads=True)
    except Exception:
        pass

    def sys_hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        crash_logger.write_exception(
            "Необработанное исключение верхнего уровня",
            exc_type,
            exc_value,
            exc_traceback,
        )
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        crash_logger.write_exception(
            f"Необработанное исключение в потоке {args.thread.name if args.thread else 'unknown'}",
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
        )
        threading.__excepthook__(args)

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook
    return crash_logger


def bind_tk_crash_logging(app: object, crash_logger: CrashLogger) -> None:
    try:
        import tkinter.messagebox as messagebox
    except Exception:
        messagebox = None

    def report_callback_exception(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        crash_logger.write_exception(
            "Необработанное исключение Tkinter callback",
            exc_type,
            exc_value,
            exc_traceback,
        )
        if messagebox is not None:
            try:
                messagebox.showerror(
                    "DataFusion RT",
                    f"Произошла ошибка. Crash log:\n{crash_logger.path}",
                    parent=app,
                )
            except Exception:
                pass

    setattr(app, "report_callback_exception", report_callback_exception)
