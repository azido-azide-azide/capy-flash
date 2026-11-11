"""reqlog.py unit tests: every line of samples/ninfer-serve.sample.log (real, engine v0.7.7)
plus synthetic lines (grammar from src/serve/operational_log.cpp renderers) for the
line types the sampled session never emitted. Run: python3 tests/test_reqlog.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reqlog import (ReqLog, LogRing, NinferLevelReader, ENVELOPE,
                    pcount, pdur, prate, ppct)

SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fixtures", "ninfer-serve.sample.log")


class TestValueParsers(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(pcount("51,220"), 51220)
        self.assertEqual(pcount("0"), 0)
        self.assertIsNone(pcount("x,2"))

    def test_durations(self):
        self.assertAlmostEqual(pdur("900 us"), 0.0009)
        self.assertAlmostEqual(pdur("28.0 ms"), 0.028)
        self.assertAlmostEqual(pdur("173 ms"), 0.173)
        self.assertAlmostEqual(pdur("48.5s"), 48.5)
        self.assertAlmostEqual(pdur("4m 2.9s"), 242.9)
        self.assertAlmostEqual(pdur("1h 5m"), 3900.0)
        self.assertIsNone(pdur("junk"))
        self.assertIsNone(pdur(None))

    def test_rates(self):
        self.assertAlmostEqual(prate("2.55k tok/s"), 2550.0)
        self.assertAlmostEqual(prate("106.6 tok/s"), 106.6)
        self.assertAlmostEqual(prate("1.2M tok/s"), 1.2e6)
        self.assertIsNone(prate("5 apples/s"))

    def test_percents(self):
        self.assertAlmostEqual(ppct("84.7%"), 0.847)
        self.assertAlmostEqual(ppct("100.0%"), 1.0)
        self.assertIsNone(ppct("n/a%"))


class TestSampleLog(unittest.TestCase):
    """The 14 real lines from /var/log/ninfer-serve.log (tail -300 window)."""

    @classmethod
    def setUpClass(cls):
        cls.lines = [l for l in open(SAMPLE, encoding="utf-8").read().splitlines() if l.strip()]
        cls.rl = ReqLog()
        cls.events = [cls.rl.feed(l) for l in cls.lines]
        cls.types = [e[0] if e else None for e in cls.events]

    def test_sample_size(self):
        self.assertEqual(len(self.lines), 14)

    def test_every_line_parses(self):
        for l, e in zip(self.lines, self.events):
            self.assertIsNotNone(e, f"line did not parse: {l!r}")
            self.assertIsNotNone(e[0])

    def test_listening_starts_session_and_model(self):
        self.assertEqual(self.rl.model, "qwen3.8-27b-quasar")
        self.assertEqual(self.rl.engine_state, "down")      # sample ends with `server stopped`
        self.assertIn("listening", self.types)
        self.assertGreaterEqual(self.rl.session, 1)

    def test_capacity_kv_tokens(self):
        self.assertEqual(self.rl.kv_tokens, 262144)

    def test_started_record(self):
        i = self.types.index("started")
        ev = self.events[i][1]
        self.assertEqual(ev["id"], "7")
        self.assertEqual(ev["api"], "openai-chat")
        self.assertEqual(ev["mode"], "stream")
        self.assertEqual(pcount(ev["messages"]), 28)
        self.assertEqual(pcount(ev["tools"]), 19)
        self.assertEqual(ev["thinking"].strip(), "xhigh, budget 32,768")

    def test_done_full_clause_parse(self):
        evs = [e[1] for e, t in zip(self.events, self.types) if t == "done"]
        r17 = next(r for r in evs if r["id"] == "req#17")
        self.assertEqual(r17["prompt_tokens"], 48092)
        self.assertEqual(r17["output_tokens"], 2756)
        self.assertEqual(r17["reused"], 47138)
        self.assertAlmostEqual(r17["cache_pct"], 0.98)
        self.assertEqual(r17["cache_path"], "private endpoint")
        self.assertAlmostEqual(r17["ttft_s"], 0.515)
        self.assertAlmostEqual(r17["duration_s"], 26.4)
        self.assertAlmostEqual(r17["queue_s"], 0.012)
        self.assertAlmostEqual(r17["decode_tok_s"], 106.6)
        self.assertEqual(r17["spec_backend"], "dflash2")
        self.assertEqual(r17["drafts_accepted"], 1916)
        self.assertEqual(r17["drafts_offered"], 4205)
        self.assertEqual(r17["thinking_tokens"], 2487)
        self.assertEqual(r17["finish"], "stop")           # "tool calls 1" -> stop
        self.assertIsNotNone(r17["prompt_ms"])
        # prompt_ms = (prompt-reused)/prefill_rate: 954 tok @ 2.06k tok/s ~ 463 ms
        self.assertAlmostEqual(r17["prompt_ms"], 954 / 2060.0 * 1000, delta=2)

    def test_done_long_durations_and_zero_cache(self):
        r3 = next(e[1] for e, t in zip(self.events, self.types) if t == "done" and e[1]["id"] == "req#3")
        self.assertAlmostEqual(r3["duration_s"], 242.9)     # 4m 2.9s
        self.assertAlmostEqual(r3["ttft_s"], 48.5)
        self.assertEqual(r3["reused"], 0)
        self.assertIsNone(r3["cache_path"])
        self.assertAlmostEqual(r3["queue_s"], 16.9)

    def test_failed_line(self):
        evs = [e[1] for e, t in zip(self.events, self.types) if t == "failed"]
        cancelled = next(e for e in evs if e["word"] == "cancelled")
        self.assertEqual(cancelled["phase"], "transport")
        self.assertEqual(cancelled["http"], "499")
        self.assertEqual(cancelled["cause"], "client disconnected")
        warnfail = next(e for e in evs if e["id"] == "24")
        self.assertEqual(warnfail["phase"], "generation")
        self.assertEqual(warnfail["http"], "503")

    def test_throughput_line(self):
        self.assertEqual(self.rl.throughput["running"], 1)
        self.assertAlmostEqual(self.rl.throughput["decode_tok_s"], 30.8)   # the newest of the two samples
        self.assertEqual(self.rl.throughput["waiting"], 0)

    def test_fatal_and_stopped(self):
        self.assertIn("server_failed", self.types)
        self.assertEqual(self.types[-1], "server_stopped")
        self.assertEqual(self.rl.engine_state, "down")

    def test_unknown_renderer_lines_are_tolerated(self):
        # tensor parallel / context cache lines carry no panel grammar: 'other', no crash
        self.assertEqual(self.types[1], "other")
        self.assertEqual(self.types[3], "other")

    def test_requests_newest_first_and_no_failures_inside(self):
        ids = [r["id"] for r in self.rl.requests]
        self.assertNotIn("req#24", ids)                    # failed requests stay in the log ring
        times = [r["time"] for r in self.rl.requests]
        self.assertEqual(times, sorted(times, reverse=True))


class TestSyntheticGrammar(unittest.TestCase):
    """Renderers the sampled session never emitted; grammar from operational_log.cpp."""

    def setUp(self):
        self.rl = ReqLog()

    def feed(self, msg, level="INFO"):
        return self.rl.feed(f"2026-11-09 12:00:00.000  {level:<5} {msg}")

    def test_prefill_miss(self):
        ev = self.feed("req#9 prefill miss | recomputed 512 of 4,096 | reused 3,584 | "
                       "common prefix 3,584 with a recent context of 4,096 | dump /tmp/ninfer-miss-9")
        self.assertEqual(ev[0], "prefill_miss")
        self.assertEqual(ev[1]["recomputed"], "512")
        self.assertEqual(pcount(ev[1]["prompt"]), 4096)

    def test_rejected_during_prepare_with_counts(self):
        ev = self.feed("req#11 rejected during prepare | openai-chat stream | HTTP 413 | "
                       "context too large | messages 12 | tools 3", "WARN")
        self.assertEqual(ev[0], "failed")
        self.assertEqual(ev[1]["word"], "rejected")
        self.assertEqual(ev[1]["http"], "413")
        self.assertEqual(ev[1]["cause"], "context too large")

    def test_tool_markup_fallback(self):
        ev = self.feed("req#12 tool markup returned as text | json parse failed", "WARN")
        self.assertEqual(ev[0], "tool_markup")
        self.assertEqual(ev[1]["reason"], "json parse failed")

    def test_http_failed(self):
        ev = self.feed("HTTP request failed during transport | anthropic | HTTP 499 | client disconnected", "WARN")
        self.assertEqual(ev[0], "http_failed")
        self.assertEqual(ev[1]["api"], "anthropic")

    def test_throughput_with_queue_states(self):
        ev = self.feed("throughput | 5.0s | prefill 1.23k tok/s (6,150 tok) | decode 101.9 tok/s (510 tok) | "
                       "running 1 (prefill 1, decode-ready 0) | waiting 2 | materializing 1 | batch 0.87 | "
                       "host 12.3% (615 ms)")
        self.assertEqual(ev[0], "throughput")
        self.assertEqual(ev[1]["waiting"], "2")

    def test_throughput_absent_half_is_zero(self):
        """The renderer omits a half when that class produced no tokens in the
        window — record 0.0 (the chart plots it on the y=0 baseline)."""
        self.feed("throughput | 1.0s | decode 30.0 tok/s (30 tok) | running 1 | batch 1.00 | host 5.0% (5.0 ms)")
        self.assertAlmostEqual(self.rl.throughput["decode_tok_s"], 30.0)
        self.assertEqual(self.rl.throughput["prefill_tok_s"], 0.0)

    def test_thinking_with_control_clause(self):
        ev = self.feed("req#20 done | anthropic | stop token | prompt 1,000 | output 500 | cache 900 (90.0%) | "
                       "TTFT 120 ms | total 6.0s | prefill 1.2k tok/s | decode 95.0 tok/s | "
                       "thinking 400/1024, control 12")
        r = ev[1]
        self.assertEqual(r["finish"], "stop")
        self.assertEqual(r["thinking_tokens"], 400)
        self.assertEqual(r["thinking_budget"], 1024)

    def test_output_limit_maps_to_length(self):
        ev = self.feed("req#21 done | openai-responses | output limit | prompt 100 | output 32,768 | "
                       "cache 0 (0.0%) | TTFT 50 ms | total 300.0s | decode 109.2 tok/s")
        self.assertEqual(ev[1]["finish"], "length")
        self.assertEqual(ev[1]["api"], "openai")
        self.assertEqual(ev[1]["path"], "/v1/responses")

    def test_session_reset_on_relisten(self):
        self.feed("listening on http://0.0.0.0:8001 | model qwen3.8-27b-quasar | auth disabled")
        s1 = self.rl.session
        self.feed("req#1 started | openai-chat stream | 1 messages | max output 100 | thinking off")
        self.assertEqual(len(self.rl.inflight), 1)
        self.feed("listening on http://0.0.0.0:8001 | model qwen3.8-27b-quasar | auth disabled")
        self.assertEqual(self.rl.session, s1 + 1)
        self.assertEqual(len(self.rl.inflight), 0)          # restart clears dead in-flight rows
        self.feed("req#1 started | openai-chat stream | 1 messages | max output 100 | thinking off")
        self.assertEqual(len(self.rl.inflight), 1)          # id 1 is reusable in the new session

    def test_garbage_never_raises(self):
        for junk in ["", "not a log line", "2026-13-45 99:99:99.999 INFO req#x done",
                     "2026-11-09 12:00:00.000  INFO  req#7 done | x | weird | prompt abc"]:
            self.rl.feed(junk)                              # must not raise


class TestLogRing(unittest.TestCase):
    def test_ring_keeps_levels_and_times(self):
        from logbuf import LogBuffer
        from loglevel import INFO, WARN, ERR
        ring = LogRing(LogBuffer(level_reader_factory=NinferLevelReader))
        for l in open(SAMPLE, encoding="utf-8").read().splitlines():
            if l.strip():
                ring.add_line(l)
        lines, last = ring.buffer.since(0, 500)
        self.assertEqual(len(lines), 14)
        self.assertEqual(last, 14)
        levels = ring.buffer.levels()
        self.assertEqual(levels[WARN], 1)                   # the WARN failed-generation line
        self.assertEqual(levels[ERR], 1)                    # the FATAL startup failure
        self.assertEqual(levels[INFO], 12)
        # stamped with the engine clock, not the ingest clock (they differ by days)
        self.assertLess(abs(lines[0]["t"] - lines[-1]["t"]), 4 * 3600)

    def test_level_lines_fix_marker_present(self):
        from logbuf import LogBuffer
        self.assertTrue(hasattr(LogBuffer, "level_lines"))   # the fix we are porting


if __name__ == "__main__":
    unittest.main(verbosity=2)
