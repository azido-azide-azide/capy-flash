"""follow.py + jsonl.py tests: tail mechanics (append, shrink), inflight
driving from log events, and the JSONL (schema-v28) reader against the exact
field names of src/serve/request_log.cpp.
Run: python3 tests/test_follow.py"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from follow import _drive_inflight, tail_log
from jsonl import JsonlIngest
from metrics import DashState
from reqlog import NinferLevelReader
from logbuf import LogBuffer
from reqlog import LogRing

ENV = "2026-11-09 12:00:00.000  INFO  "
MSG_START = "req#7 started | openai-chat stream | 3 messages | max output 512 | thinking 0/32,768"
MSG_DONE = ("req#7 done | openai-chat | stop token | prompt 1,000 | output 300 | "
            "cache 900 (90.0%) | TTFT 1.2s | total 9.0s | queue 12 ms | decode 40.0 tok/s")
START = ENV + MSG_START
DONE = "2026-11-09 12:00:09.000  INFO  " + MSG_DONE
TROUGH = "2026-11-09 12:00:04.000  INFO  throughput | 5.00s | running 1 (decode-ready 1) | waiting 0 | host 2% (3 ms)"


class _StubTel:
    def __init__(self, state):
        state.telemetry.stop()
        state.telemetry = self

    def snapshot(self):
        return {"now": {}, "static": {}, "history": {}}

    def stop(self):
        pass


def fresh_state():
    st = DashState("/nonexistent", gpu_indices=[])
    _StubTel(st)
    return st


class TestInflightDriving(unittest.TestCase):
    def setUp(self):
        self.st = fresh_state()

    def test_lifecycle(self):
        _drive_inflight(self.st, "started", {"api": "openai-chat"})
        self.assertIsNotNone(self.st.inflight)
        self.assertEqual(self.st.inflight["path"], "/v1/chat/completions")
        self.assertEqual(self.st.inflight["api"], "openai")
        self.assertIsNone(self.st.inflight["first"])
        _drive_inflight(self.st, "throughput", {"run_detail": "decode-ready 1"})
        self.assertIsNotNone(self.st.inflight["first"])
        _drive_inflight(self.st, "done", {})
        self.assertIsNone(self.st.inflight)

    def test_restart_clears_stale_inflight(self):
        _drive_inflight(self.st, "started", {"api": "anthropic"})
        _drive_inflight(self.st, "server_stopped", {})
        self.assertIsNone(self.st.inflight)

    def test_prefill_only_keeps_reading_state(self):
        _drive_inflight(self.st, "started", {"api": "openai-chat"})
        _drive_inflight(self.st, "throughput", {"run_detail": "prefill 1"})
        self.assertIsNone(self.st.inflight["first"])


class TestTailLog(unittest.TestCase):
    def test_append_then_shrink(self):
        st = fresh_state()
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            path = f.name
            f.write("old line that must NOT be replayed\n")
        try:
            t = threading.Thread(target=tail_log, args=(st, path),
                                 kwargs={"poll_s": 0.02}, daemon=True)
            t.start()
            time.sleep(0.15)                        # tail now sits at EOF
            with open(path, "a") as f:
                f.write(START + "\n")
            self._wait_for(lambda: st.inflight is not None)
            self.assertEqual(st.inflight["api"], "openai")
            open(path, "w").close()                 # rotation/trim: size shrinks → reset to 0
            time.sleep(0.1)                         # let the poller observe the shrink
            with open(path, "a") as f:              # a rewrite big enough to hide above the
                f.write(DONE + "\n")                # old offset is undetectable-by-design
            self._wait_for(lambda: st.reqlog.requests)
            self.assertEqual(st.reqlog.requests[0]["id"], "req#7")
            self.assertEqual(st.reqlog.requests[0]["output_tokens"], 300)
            msgs = [l["msg"] for l in st.ring.buffer.since(0)[0]]
            self.assertNotIn("old line that must NOT be replayed", msgs)
        finally:
            os.unlink(path)

    @staticmethod
    def _wait_for(cond, timeout=4.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.02)
        raise AssertionError("condition not reached")


class TestJsonl(unittest.TestCase):
    REQ_KEYS = {"id", "session", "time", "slot", "path", "api", "output_tokens",
                "decode_tok_s", "duration_s", "finish", "prompt_tokens", "reused",
                "prompt_ms", "ttft_s", "queue_s", "cache_pct", "cache_path",
                "spec_backend", "drafts_accepted", "drafts_offered",
                "thinking_tokens", "thinking_budget"}

    @staticmethod
    def _base(event):
        return {"artifact_type": "ninfer_serve_request_log", "schema_version": 28,
                "event": event, "timestamp_unix_ms": 1794224096789,
                "server_instance_id": "inst-A"}

    def test_request_lifecycle_shapes(self):
        ing = JsonlIngest()
        start = dict(self._base("request_start"),
                     request={"request_id": 7, "protocol": "openai-chat", "stream": True})
        self.assertEqual(ing.event(start), ("started", {"api": "openai-chat"}))
        done = dict(self._base("request_done"),
                    request={"request_id": 7, "protocol": "openai-chat"},
                    result={"finish_reason": "output_limit", "prompt_tokens": 1000,
                            "completion_tokens": 300, "computed_prefill_tokens": 100,
                            "prefix_cache_hit_tokens": 900, "prefix_reuse_path": "private_endpoint",
                            "model_thinking_tokens": 40, "thinking_budget": 32768},
                    timings_seconds={"prepare": 0.01, "ttft": 1.234, "vision": None,
                                     "prefill": 0.25, "decode": 7.5, "total": 9.0},
                    speculative={"backend": "dflash2", "drafted_tokens": 500,
                                 "accepted_tokens": 200})
        kind, req = ing.event(done)
        self.assertEqual(kind, "done")
        self.assertEqual(set(req.keys()), self.REQ_KEYS)    # same shape as the text parser
        self.assertEqual(req["id"], "req#7")
        self.assertEqual(req["finish"], "length")
        self.assertEqual(req["decode_tok_s"], 40.0)
        self.assertEqual(req["prompt_ms"], 250.0)   # measured prefill seconds → ms
        self.assertEqual(req["reused"], 900)
        self.assertEqual(req["cache_pct"], 90.0)
        self.assertEqual(req["cache_path"], "private_endpoint")
        self.assertEqual(req["drafts_accepted"], 200)
        self.assertEqual(req["drafts_offered"], 500)
        self.assertEqual(req["thinking_tokens"], 40)
        self.assertEqual(req["time"], 1794224096.789)
        self.assertEqual(ing.inflight, {})        # container stays; the entry was popped

    def test_error_event_is_failed(self):
        ing = JsonlIngest()
        ev = ing.event(dict(self._base("request_error"),
                             request={"request_id": 8, "protocol": "anthropic"},
                             error={"message": "cancelled"}))
        self.assertEqual(ev, ("failed", {"id": 8}))

    def test_throughput_and_server_start(self):
        ing = JsonlIngest()
        kind, fields = ing.event(dict(self._base("throughput"), prefill=10000, decode=2000,
                                      elapsed_seconds=5.0, running=1, waiting=0,
                                      decode_ready=1, decode_batch={"average_size": 1.0}))
        self.assertEqual(kind, "throughput")
        self.assertEqual(fields, {"run_detail": "decode-ready"})
        self.assertEqual(ing.throughput["decode_tok_s"], 400.0)
        self.assertEqual(ing.throughput["prefill_tok_s"], 2000.0)
        ing.event(dict(self._base("server_start"), model="qwen3.8-27b-quasar",
                       context_capacity={"tokens": 262144}))
        self.assertEqual(ing.model, "qwen3.8-27b-quasar")
        self.assertEqual(ing.kv_tokens, 262144)

    def test_garbage_and_wrong_artifact_rejected(self):
        ing = JsonlIngest()
        self.assertIsNone(ing.event({"artifact_type": "other", "schema_version": 28}))
        self.assertIsNone(ing.event({"artifact_type": "ninfer_serve_request_log",
                                     "schema_version": 1}))
        self.assertIsNone(ing.event("not a dict"))
        self.assertEqual(ing.bad_lines, 3)


class TestJsonlDrivesInflight(unittest.TestCase):
    def test_jsonl_events_open_and_close_inflight(self):
        st = fresh_state()
        ing = JsonlIngest()
        _drive_inflight(st, *ing.event({"artifact_type": "ninfer_serve_request_log",
                                        "schema_version": 28, "event": "request_start",
                                        "timestamp_unix_ms": 1, "server_instance_id": "i",
                                        "request": {"request_id": 1,
                                                    "protocol": "openai-chat"}}))
        self.assertIsNotNone(st.inflight)
        _drive_inflight(st, *ing.event({"artifact_type": "ninfer_serve_request_log",
                                        "schema_version": 28, "event": "request_done",
                                        "timestamp_unix_ms": 2, "server_instance_id": "i",
                                        "request": {"request_id": 1, "protocol": "openai-chat"},
                                        "result": {"finish_reason": "stop_token",
                                                   "prompt_tokens": 10, "completion_tokens": 5},
                                        "timings_seconds": {}, "speculative": {}}))
        self.assertIsNone(st.inflight)


if __name__ == "__main__":
    unittest.main(verbosity=2)
