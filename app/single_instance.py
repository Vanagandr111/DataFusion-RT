from __future__ import annotations

import atexit
import ctypes
from dataclasses import dataclass


ERROR_ALREADY_EXISTS = 183


@dataclass
class SingleInstanceGuard:
    handle: int
    name: str

    def release(self) -> None:
        if self.handle:
            ctypes.windll.kernel32.ReleaseMutex(self.handle)
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = 0


def acquire_single_instance(name: str) -> tuple[bool, SingleInstanceGuard | None]:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return True, None
    last_error = kernel32.GetLastError()
    if last_error == ERROR_ALREADY_EXISTS:
        ctypes.windll.kernel32.CloseHandle(handle)
        return False, None
    guard = SingleInstanceGuard(handle=handle, name=name)
    atexit.register(guard.release)
    return True, guard
