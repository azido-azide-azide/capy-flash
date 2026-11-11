"""Regression tests for the JSONL ledger reader against REAL schema-v27 records.

The fixture (fixtures/ninfer-requests.sample.jsonl) is a verbatim pull from the
live CT131 ledger — one line per event kind, including the prefill+decode and
decode-only throughput shapes. These tests exist because the first JSONL reader
was written pre-flight with guessed flat field names and stayed green on
synthetic fixtures while the live graphs sat flat (PR #5).
"""
from __future__ import annotations

import json
import os
import unittest

from follow import _drive_inflight
from jsonl import JsonlIngest

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "ninfer-requests.sample.jsonl")


def sample() -> list[dict]:
    with open(SAMPLE, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def feed(ing: JsonlIngest, rec: dict):
    ev = ing.event(rec)
    assert ev is not None
    return ev


class TestThroughputMapping(unittest.TestCase):
    def test_prefill_and_decode_rates(self):
        ing = JsonlIngest()
        rec = next(r for r in sample()
                   if r["event"] == "throughput" and r["throughput_tokens_per_second"]["prefill"] > 0)
        ev = feed(ing, rec)
        self.assertEqual(ev[0], "throughput")
        tp = ing.throughput
        self.assertAlmostEqual(tp["decode_tok_s"], round(rec["throughput_tokens_per_second"]["decode"], 1))
        self.assertAlmostEqual(tp["prefill_tok_s"], round(rec["throughput_tokens_per_second"]["prefill"], 1))
        self.assertEqual(tp["running"], rec["scheduler"]["running"])
        self.assertEqual(tp["waiting"], rec["scheduler"]["waiting"])
        self.assertEqual(tp["batch"], rec["decode_batch"]["average_size"])
        self.assertAlmostEqual(ing.throughput_at, rec["timestamp_unix_ms"] / 1000.0)

    def test_zero_prefill_maps_to_zero(self):
        """A 0.0 rate is kept as 0.0 (the chart plots it on the y=0 baseline,
        keeping the line continuous) — not dropped to a gap."""
        ing = JsonlIngest()
        rec = next(r for r in sample()
                   if r["event"] == "throughput" and r["throughput_tokens_per_second"]["prefill"] == 0
                   and r["throughput_tokens_per_second"]["decode"] > 0)
        feed(ing, rec)
        self.assertEqual(ing.throughput["prefill_tok_s"], 0.0)
        self.assertIsNotNone(ing.throughput["decode_tok_s"])

    def test_missing_rate_key_stays_none(self):
        ing = JsonlIngest()
        rec = next(r for r in sample() if r["event"] == "throughput")
        del rec["throughput_tokens_per_second"]["prefill"]
        feed(ing, rec)
        self.assertIsNone(ing.throughput["prefill_tok_s"])

    def test_run_detail_from_scheduler(self):
        ing = JsonlIngest()
        rec = next(r for r in sample()
                   if r["event"] == "throughput" and r["scheduler"]["decode_ready"] > 0)
        ev = feed(ing, rec)
        self.assertIn("decode-ready", str(ev[1].get("run_detail", "")))

    def test_drive_inflight_marks_generation(self):
        """The in-flight view must flip reading→generating on a decode-ready
        throughput line — the observer has no SSE, this is the only signal."""
        class State:
            def __init__(self):
                self.inflight = None
            def begin_request(self, path, api):
                self.inflight = {"path": path, "api": api, "started": 0.0, "first": None, "chunks": 0}
            def end_request(self):
                self.inflight = None

        st = State()
        start = next(r for r in sample() if r["event"] == "request_start")
        tp = next(r for r in sample()
                  if r["event"] == "throughput" and r["scheduler"]["decode_ready"] > 0)
        ing = JsonlIngest()
        _drive_inflight(st, *feed(ing, start))
        self.assertIsNone(st.inflight["first"])
        _drive_inflight(st, *feed(ing, tp))
        self.assertIsNotNone(st.inflight["first"])


class TestServerStartMapping(unittest.TestCase):
    def test_model_and_kv_capacity(self):
        ing = JsonlIngest()
        rec = next(r for r in sample() if r["event"] == "server_start")
        ev = feed(ing, rec)
        self.assertEqual(ev[0], "listening")
        self.assertEqual(ing.model, rec["artifact"]["name"])
        self.assertEqual(ing.kv_tokens, rec["engine"]["kv_capacity"])
        self.assertEqual(ing.engine_state, "up")


class TestRequestLifecycle(unittest.TestCase):
    def test_start_maps_protocol(self):
        ing = JsonlIngest()
        rec = next(r for r in sample() if r["event"] == "request_start")
        ev = feed(ing, rec)
        self.assertEqual(ev[0], "started")
        self.assertEqual(ev[1]["api"], "openai")
        key = (ing.sessions[rec["server_instance_id"]], rec["request"]["request_id"])
        self.assertIn(key, ing.inflight)

    def test_done_fields(self):
        ing = JsonlIngest()
        rec = next(r for r in sample() if r["event"] == "request_done")
        ev = feed(ing, rec)
        self.assertEqual(ev[0], "done")
        done = ev[1]
        res, tim = rec["result"], rec["timings_seconds"]
        self.assertEqual(done["path"], "/v1/chat/completions")
        self.assertEqual(done["api"], "openai")
        self.assertEqual(done["output_tokens"], res["completion_tokens"])
        self.assertEqual(done["prompt_tokens"], res["prompt_tokens"])
        self.assertEqual(done["reused"], res["prefix_cache_hit_tokens"])
        self.assertAlmostEqual(done["queue_s"],
                               round(rec["engine_timing"]["queue_wait_seconds"], 3))
        self.assertAlmostEqual(done["decode_tok_s"],
                               round(res["completion_tokens"] / tim["decode"], 1))
        self.assertAlmostEqual(done["ttft_s"], round(tim["ttft"], 3))
        self.assertEqual(done["cache_path"], res["prefix_reuse_path"])
        self.assertEqual(done["spec_backend"], rec["speculative"]["backend"])

    def test_error_is_failed(self):
        ing = JsonlIngest()
        rec = next(r for r in sample() if r["event"] == "request_error")
        ev = feed(ing, rec)
        self.assertEqual(ev[0], "failed")


class TestFailSoft(unittest.TestCase):
    def test_foreign_artifact_rejected(self):
        ing = JsonlIngest()
        self.assertIsNone(ing.event({"artifact_type": "other", "event": "throughput"}))
        self.assertEqual(ing.bad_lines, 1)

    def test_old_schema_rejected(self):
        ing = JsonlIngest()
        rec = sample()[0]
        rec["schema_version"] = 26
        self.assertIsNone(ing.event(rec))


if __name__ == "__main__":
    unittest.main()
