"""logbuf.py tests: the vendored basalt ring (serve/util/logbuf.py @ cc0a763)
plus the one capy-dash patch (level_reader_factory) and LogRing envelope times.
Run: python3 tests/test_logbuf.py"""
import os
import sys
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logbuf import LogBuffer
from loglevel import ERR, INFO, WARN
from reqlog import LogRing, NinferLevelReader

def env(level, msg):                        # real spdlog envelope: date time.  LEVEL  msg
    return f"2026-11-09 12:34:56.789  {level}  {msg}"


class TestRing(unittest.TestCase):
    def test_ids_rise_and_since(self):
        b = LogBuffer(capacity=100)
        b.add("eng", "one\ntwo\n\nthree")     # blank lines skipped
        lines, last = b.since(0)
        self.assertEqual([l["msg"] for l in lines], ["one", "two", "three"])
        self.assertEqual([l["id"] for l in lines], [1, 2, 3])
        self.assertEqual(last, 3)
        fresh, _ = b.since(2)
        self.assertEqual([l["msg"] for l in fresh], ["three"])

    def test_capacity_evicts_but_ids_continue(self):
        b = LogBuffer(capacity=3)
        for i in range(6):
            b.add("eng", f"line{i}")
        lines, last = b.since(0)
        self.assertEqual([l["msg"] for l in lines], ["line3", "line4", "line5"])
        self.assertEqual([l["id"] for l in lines], [4, 5, 6])
        self.assertEqual(last, 6)

    def test_levels_counts_and_filtered_windows(self):
        b = LogBuffer(capacity=100, level_reader_factory=NinferLevelReader)
        b.add("eng", env("INFO", "throughput | 5.00s"))
        b.add("eng", env("WARN", "req#1 failed during generation | openai-chat | HTTP 503"))
        b.add("eng", env("ERROR", "startup failed | initializing CUDA | 12 ms"))
        self.assertEqual(b.levels(), {INFO: 1, WARN: 1, ERR: 1})
        warns, _ = b.level_lines("warn", 0, 500)
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[0]["lvl"], "warn")
        # counts outlive the ring:
        b2 = LogBuffer(capacity=1, level_reader_factory=NinferLevelReader)
        b2.add("eng", env("INFO", "a"))
        b2.add("eng", env("WARN", "b"))
        self.assertEqual(b2.levels(), {INFO: 1, WARN: 1, ERR: 0})
        self.assertEqual(len(b2.lines), 1)

    def test_ninfer_envelope_levels_not_message_markers(self):
        b = LogBuffer(level_reader_factory=NinferLevelReader)
        b.add("eng", env("INFO", "loading weights 45%"))      # no message markers at all
        lines, _ = b.since(0)
        self.assertEqual(lines[0]["lvl"], INFO)
        b.add("eng", env("TRACE", "fine"))                    # TRACE maps to info
        lines, _ = b.since(1)
        self.assertEqual(lines[0]["lvl"], INFO)


class TestLogRing(unittest.TestCase):
    def test_envelope_time_stamps_entries(self):
        ring = LogRing(LogBuffer(level_reader_factory=NinferLevelReader))
        before = round(time.time(), 3)
        ring.add_line(env("INFO", "hello"))
        lines, _ = ring.buffer.since(0)
        t = lines[0]["t"]
        # The entry must carry the ENVELOPE stamp (2026-11-09 12:34:56.789), not
        # the wall clock. Compare against that wall time read as UTC: any real
        # tz parse lands within ±15 h of it, and the fixture date recedes from
        # "now" forever, so this never rots (a fixed window from `before` did).
        utc_ts = datetime(2026, 11, 9, 12, 34, 56, 789000, tzinfo=timezone.utc).timestamp()
        self.assertLess(abs(t - utc_ts), 15 * 3600)
        self.assertGreater(abs(t - before), 24 * 3600)

    def test_blank_lines_dropped_and_src_tagged(self):
        ring = LogRing(LogBuffer(level_reader_factory=NinferLevelReader))
        ring.add_line("   ")
        ring.add_line("real line", src="srv")
        lines, _ = ring.buffer.since(0)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["src"], "srv")


if __name__ == "__main__":
    unittest.main(verbosity=2)
