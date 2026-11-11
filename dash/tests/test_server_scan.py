"""Tests for the startup back-scan that re-seeds the latest `server_start`
(model + KV capacity) when a JSONL engine session outlives the 500-line seed
window — the gap that left /metrics `engine.context` null between engine
restarts and dash restarts (PR #6)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from server import _lines_backwards, latest_server_start


def _rec(event: str, **over) -> str:
    base = {"artifact_type": "ninfer_serve_request_log", "schema_version": 27,
            "event": event, "timestamp_unix_ms": 1, "server_instance_id": "i",
            "throughput_tokens_per_second": {"decode": 1.0, "prefill": 0.0},
            "scheduler": {"running": 0, "waiting": 0, "decode_ready": 0}}
    base.update(over)
    return json.dumps(base) + "\n"


def _server_start(instance: str, name: str, kv: int) -> str:
    return json.dumps({"artifact_type": "ninfer_serve_request_log",
                       "schema_version": 27, "event": "server_start",
                       "timestamp_unix_ms": 1, "server_instance_id": instance,
                       "artifact": {"name": name},
                       "engine": {"kv_capacity": kv}}) + "\n"


class TestLinesBackwards(unittest.TestCase):
    def _write(self, data: bytes) -> str:
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        self.addCleanup(os.unlink, path)
        return path

    def test_all_lines_newest_first_exactly_once(self):
        data = b"".join(f"line-{i}\n".encode() for i in range(10))
        p = self._write(data)
        got = [l.decode() for l in _lines_backwards(p, chunk=13)]  # tiny chunks force straddles
        self.assertEqual(got, [f"line-{i}" for i in range(9, -1, -1)])

    def test_straddle_at_every_boundary(self):
        # lines just under the window: some head crosses every window start,
        # and a line can straddle at most one boundary (30 < 37)
        data = b"".join(b"x" * 30 + b"\n" for _ in range(7))
        p = self._write(data)
        got = [l for l in _lines_backwards(p, chunk=37)]
        self.assertEqual(got, [b"x" * 30] * 7)

    def test_partial_line_at_eof_yielded_best_effort(self):
        p = self._write(b"abc\n" + b"{" * 5)  # in-flight write
        got = list(_lines_backwards(p, chunk=10))  # one window covers the file
        self.assertEqual(got, [b"{" * 5, b"abc"])

    def test_file_starting_mid_line(self):
        # file begins mid-line: the first available piece is best-effort
        p = self._write(b"TRUNC" + b"rest\n" + b"whole\n")
        got = list(_lines_backwards(p, chunk=5))
        self.assertEqual(got, [b"whole", b"RUNCrest", b"T"])

    def test_empty_file(self):
        self.assertEqual(list(_lines_backwards(self._write(b""), chunk=8)), [])


class TestLatestServerStart(unittest.TestCase):
    def _write(self, data: bytes) -> str:
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        self.addCleanup(os.unlink, path)
        return path

    def test_returns_most_recent_not_oldest(self):
        old = _server_start("serve-1", "old-model", 100000)
        new = _server_start("serve-2", "new-model", 262144)
        pad = _rec("throughput") * 5000
        p = self._write(old.encode() + pad.encode() + new.encode() + pad.encode())
        got = latest_server_start(p, chunk=4096)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got["artifact"]["name"], "new-model")
        self.assertEqual(got["engine"]["kv_capacity"], 262144)

    def test_finds_record_across_chunk_boundaries(self):
        rec = _server_start("serve-9", "straddler", 262144)  # ~330 bytes
        pad = _rec("throughput") * 500
        # window bigger than a line (the real constraint), so the record
        # straddles at most one boundary, but not aligned to it
        got = latest_server_start(self._write((pad + rec).encode() * 3), chunk=512)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got["artifact"]["name"], "straddler")

    def test_none_when_absent(self):
        p = self._write(_rec("throughput").encode() * 10)
        self.assertIsNone(latest_server_start(p))

    def test_partial_trailing_line_does_not_break_scan(self):
        rec = _server_start("serve-3", "still-there", 262144)
        p = self._write((rec + _rec("throughput")).encode() + b'{"artifact_type"')
        got = latest_server_start(p, chunk=128)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got["artifact"]["name"], "still-there")

    def test_empty_file(self):
        self.assertIsNone(latest_server_start(self._write(b"")))


if __name__ == "__main__":
    unittest.main()
