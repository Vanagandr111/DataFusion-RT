"""
Legacy-safe matplotlib runtime hook for frozen Windows builds.

PyInstaller's stock hook uses `_pyi_rth_utils.secure_mkdtemp()`, which pulls
in `ctypes` on Windows. On older Win7 systems that path may fail very early
while importing `_ctypes`, before the GUI even starts.

For this project we only need an isolated writable MPLCONFIGDIR. A plain
`tempfile.mkdtemp()` is sufficient and avoids the problematic ctypes path.
"""


def _pyi_rthook():
    import atexit
    import os
    import shutil
    import tempfile

    configdir = tempfile.mkdtemp(prefix="df_mplcfg_")
    os.environ["MPLCONFIGDIR"] = configdir

    try:
        atexit.register(shutil.rmtree, configdir, ignore_errors=True)
    except OSError:
        pass


_pyi_rthook()
del _pyi_rthook
