from __future__ import annotations

import sys
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import rs485_listener as listener


class DummyLogger:
    def line(self, text: str = "") -> None:
        _ = text

    def section(self, title: str) -> None:
        _ = title


class ModbusRtuFrameParserTests(unittest.TestCase):
    def test_split_response_then_request_04_03(self) -> None:
        frames = listener.split_frames_from_hex(
            "01 04 06 02 31 01 74 00 60 5D 4D 01 03 00 15 00 03 14 0F"
        )
        self.assertEqual(
            [frame.hex(" ").upper() for frame in frames],
            [
                "01 04 06 02 31 01 74 00 60 5D 4D",
                "01 03 00 15 00 03 14 0F",
            ],
        )

    def test_split_response_then_request_03_03(self) -> None:
        frames = listener.split_frames_from_hex(
            "01 03 06 00 00 00 01 00 02 71 74 01 03 00 56 00 03 65 5B"
        )
        self.assertEqual(
            [frame.hex(" ").upper() for frame in frames],
            [
                "01 03 06 00 00 00 01 00 02 71 74",
                "01 03 00 56 00 03 65 5B",
            ],
        )

    def test_request_response_match_updates_history(self) -> None:
        profile = listener.SerialProfile("dk518_7e1", 9600, 7, "E", 1.0)
        furnace = listener.FurnaceConfig(register_pv=0)
        rs485 = listener.RS485Listener(
            port="COM9",
            profile=profile,
            timeout=0.1,
            furnace=furnace,
            logger=DummyLogger(),
            chamber_temp=None,
            setpoint_temp=None,
        )
        parser = listener.ModbusRtuFrameParser()
        frames = parser.feed(
            bytes.fromhex("01 03 00 15 00 03 14 0F 01 03 06 00 03 10 68 00 0A 60 6E"),
            log_raw=False,
        )
        for frame in frames:
            rs485.process_frame(frame)

        self.assertEqual(rs485.history[(3, 0x0015)], [3])
        self.assertEqual(rs485.history[(3, 0x0016)], [4200])
        self.assertEqual(rs485.history[(3, 0x0017)], [10])


if __name__ == "__main__":
    unittest.main()
