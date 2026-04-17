from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.crash_logging import install_crash_logging


class CrashLoggingTests(unittest.TestCase):
    def test_install_creates_log_and_writes_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            crash_logger = install_crash_logging(Path(temp_dir))
            try:
                crash_logger.write_exception(
                    "Тестовая ошибка",
                    RuntimeError,
                    RuntimeError("boom"),
                    None,
                )
            finally:
                crash_logger.close()

            self.assertTrue(crash_logger.path.exists())
            content = crash_logger.path.read_text(encoding="utf-8")
            self.assertIn("Crash session started", content)
            self.assertIn("Тестовая ошибка", content)
            self.assertIn("boom", content)
