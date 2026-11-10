"""metrics.py /metrics schema contract tests against REAL log fixtures.

The key sets asserted here are the contract dash/README.md documents; changing
one must be a deliberate edit to both README and this file.
Run: python3 tests/test_metrics.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from metrics import BEST_MIN_OUT, BEST_MIN_READ, DashState, best_and_p85
from reqlog import LogRing, NinferLevelReader
from logbuf import LogBuffer

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "fixtures", "ninfer-serve-fixture.log")

TOP_KEYS = {"engine", "live", "requests", "best", "p85", "hardware",
            "hardware_static", "history"}
ENGINE_KEYS = {"model", "context", "max_context"}
LIVE_KEYS = {"state", "load", "queued", "slots", "queue", "prompt_tokens",
             "generated", "elapsed_s", "tok_s", "prefill_tok_s_mean"}
REQ_KEYS = {"id", "session", "time", "slot", "path", "api", "output_tokens",
            "decode_tok_s", "duration_s", "finish", "prompt_tokens", "reused",
            "prompt_ms", "ttft_s", "queue_s", "cache_pct", "cache_path",
            "spec_backend", "drafts_accepted", "drafts_offered",
            "thinking_tokens", "thinking_budget"}
SLOT_KEYS = {"id", "n_ctx", "is_processing", "state", "api", "model", "tok_s",
             "generated", "max_tokens", "prompt_tokens", "prefill_done",
             "ctx_used", "reused", "ttft_s", "draft_len", "drafts_offered",
             "drafts_accepted"}


class _StubTelemetry:
    def __init__(self, state):
        state.telemetry.stop()
        state.telemetry = self

    def snapshot(self):
        return {"now": {"gpus": [], "cpu": 0.0}, "static": {"gpus": []}, "history": {}}

    def stop(self):
        pass


def make_state():
    st = DashState("/nonexistent/ninfer.log", gpu_indices=[])
    _StubTelemetry(st)
    st.engine_up = True
    with open(FIXTURE, encoding="utf-8") as f:
        for line in f:
            st.ring.add_line(line)
            st.reqlog.feed(line)
    return st


class TestSchemaContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.st = make_state()
        cls.m = cls.st.metrics()

    def test_the_engine_parsed_the_fixture(self):
        self.assertTrue(self.st.reqlog.requests, "fixture replay produced no requests")
        self.assertTrue(self.st.reqlog.model, "fixture has a `listening on` model line")

    def test_top_level_key_set(self):
        self.assertEqual(set(self.m.keys()), TOP_KEYS)
        self.assertEqual(set(self.m["engine"].keys()), ENGINE_KEYS)
        self.assertEqual(set(self.m["live"].keys()), LIVE_KEYS)

    def test_request_row_key_set(self):
        for r in self.m["requests"]:
            self.assertEqual(set(r.keys()), REQ_KEYS)
        self.assertLessEqual(len(self.m["requests"]), 12)   # REQUESTS_SHOWN

    def test_id_and_api_shapes(self):
        r = self.m["requests"][0]
        self.assertRegex(r["id"], r"^req#\d+$")
        self.assertIn(r["api"], ("openai", "anthropic"))
        self.assertIn(r["finish"], ("stop", "length", "context capacity", "cancelled"))

    def test_best_and_p85_are_gated(self):
        for k, entry in self.m["best"].items():
            self.assertEqual(set(entry.keys()),
                             {"tok_s", "id", "time",
                              "output_tokens" if k == "decode" else "read_tokens"})
        for k, entry in self.m["p85"].items():
            self.assertEqual(set(entry.keys()), {"tok_s", "n", "min_tokens"})
            gate = BEST_MIN_OUT if k == "decode" else BEST_MIN_READ
            self.assertEqual(entry["min_tokens"], gate)
            self.assertGreaterEqual(entry["n"], 1)

    def test_single_slot(self):
        self.assertEqual(self.m["live"]["slots"]["total"], 1)
        row = self.st.slot_row()
        self.assertEqual(set(row.keys()), SLOT_KEYS)
        self.assertEqual(row["id"], 0)

    def test_engine_identity_from_log(self):
        self.assertEqual(self.m["engine"]["model"], self.st.reqlog.model)
        self.assertEqual(self.m["engine"]["context"], self.m["engine"]["max_context"])


class TestBestAndP85(unittest.TestCase):
    def _req(self, id, out, dec, prompt, reused, prompt_ms):
        return {"id": id, "time": 0.0, "output_tokens": out, "decode_tok_s": dec,
                "prompt_tokens": prompt, "reused": reused, "prompt_ms": prompt_ms}

    def test_gates_exclude_small_requests(self):
        reqs = [self._req("req#1", 10, 999.0, 100, 0, 1.0),        # both below gates
                self._req("req#2", 300, 40.0, 9000, 4000, 500.0)]  # both pass
        best, p85 = best_and_p85(list(reversed(reqs)))             # newest first
        self.assertEqual(best["decode"]["id"], "req#2")
        self.assertEqual(best["prefill"]["id"], "req#2")
        self.assertEqual(p85["decode"]["n"], 1)

    def test_nearest_rank_p85(self):
        reqs = [self._req(f"req#{i}", 300, float(i), 5000, 0, 100.0) for i in range(1, 101)]
        _, p85 = best_and_p85(list(reversed(reqs)))
        self.assertEqual(p85["decode"]["tok_s"], 85.0)
        self.assertEqual(p85["decode"]["n"], 100)

    def test_best_prefers_max_decode(self):
        reqs = [self._req("req#1", 300, 41.5, 9000, 0, 200.0),
                self._req("req#2", 300, 44.2, 9000, 0, 200.0)]
        best, _ = best_and_p85(list(reversed(reqs)))
        self.assertEqual(best["decode"]["id"], "req#2")
        self.assertEqual(best["decode"]["tok_s"], 44.2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
