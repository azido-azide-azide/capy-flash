"""GET /metrics payload assembly for the Capy dashboard (A1 sidecar design).

Keys mirror basalt serve/service.py `metrics()` (main@cc0a763) exactly, so the copied
serve/web/app.js renders them unchanged:

    engine            <- ReqLog model/kv_tokens (from `listening on` / `capacity` lines)
    live              <- the proxy's in-flight view (SSE chunk counting) + last throughput line
    requests          <- ReqLog parsed `req#N done` lines, newest first (max REQUESTS_SHOWN)
    best / p85        <- same gates and nearest-rank p85 as basalt (ported below verbatim)
    hardware / history / hardware_static <- telemetry.Telemetry (copied unmodified), fed
                        tok_s / slot_tok_s series through its `extra()` hook

Single stream: capy-flash runs --max-concurrency 1, so live.slots.total is always 1 and
the multi-slot panels of app.js stay dormant (multi = total > 1 = False).
"""
from __future__ import annotations

import time

from logbuf import LogBuffer
from reqlog import LogRing, NinferLevelReader, ReqLog
from telemetry import Telemetry

REQUESTS_SHOWN = 12          # basalt service.py: requests shown by /metrics
BEST_MIN_OUT = 256           # output tokens, for the best/p85 decode rate
BEST_MIN_READ = 4096         # newly read prompt tokens, for the best/p85 prefill rate
THROUGHPUT_FRESH_S = 15.0    # engine prints a `throughput` line every 5 s; older = stale


def best_and_p85(requests: list[dict]) -> tuple[dict, dict]:
    """Ported from serve/service.py metrics(): `best` scan + the p85 nearest-rank over the
    same minimum-size gates, so a wild short-request rate can never stand beside best."""
    best: dict = {}
    for r in reversed(requests):                      # oldest first
        if (r.get("output_tokens") or 0) >= BEST_MIN_OUT and r.get("decode_tok_s") and \
                r["decode_tok_s"] > best.get("decode", {}).get("tok_s", 0):
            best["decode"] = {"tok_s": r["decode_tok_s"], "id": r["id"], "time": r["time"],
                              "output_tokens": r["output_tokens"]}
        read = (r.get("prompt_tokens") or 0) - (r.get("reused") or 0)
        if read >= BEST_MIN_READ and r.get("prompt_ms"):
            rate = round(read / (r["prompt_ms"] / 1000), 1)
            if rate > best.get("prefill", {}).get("tok_s", 0):
                best["prefill"] = {"tok_s": rate, "id": r["id"], "time": r["time"], "read_tokens": read}

    dec = sorted(r["decode_tok_s"] for r in requests
                 if (r.get("output_tokens") or 0) >= BEST_MIN_OUT and r.get("decode_tok_s"))
    pre = sorted((r["prompt_tokens"] - r["reused"]) * 1000 / r["prompt_ms"]
                 for r in requests
                 if (r.get("prompt_tokens") or 0) - (r.get("reused") or 0) >= BEST_MIN_READ
                 and r.get("prompt_ms"))

    def nearest_rank(v):
        return v[(len(v) * 85 + 99) // 100 - 1]

    p85 = {k: {"tok_s": round(nearest_rank(v), 1), "n": len(v), "min_tokens": gate}
           for k, v, gate in (("decode", dec, BEST_MIN_OUT), ("prefill", pre, BEST_MIN_READ)) if v}
    return best, p85


class DashState:
    """The single source of dashboard truth: log ring + parsed req state + in-flight view."""

    def __init__(self, log_path: str, gpu_indices=(0, 1), api_key: str = "",
                 model_hint: str = ""):
        self.started_at = time.time()
        self.log_path = log_path
        self.api_key = api_key
        self.reqlog = ReqLog()
        self.ring = LogRing(LogBuffer(level_reader_factory=NinferLevelReader))
        self.telemetry = Telemetry(extra=self._extra, gpu_indices=list(gpu_indices))
        self.inflight: dict | None = None            # set by the proxy per generation
        self.engine_up: bool | None = None           # tri-state until the poller answers
        self.model_hint = model_hint

    def stop(self):
        self.telemetry.stop()

    # -- in-flight accounting (driven by the proxy's SSE pump) ----------------
    def begin_request(self, path: str, api: str):
        self.inflight = {"path": path, "api": api, "started": time.time(),
                         "first": None, "chunks": 0}

    def sse_data(self, n: int = 1):
        infl = self.inflight
        if infl is None:
            return
        if infl["first"] is None:
            infl["first"] = time.time()
        infl["chunks"] += n

    def end_request(self):
        self.inflight = None                          # `req#N done` lands in the log ~immediately

    def _live_tok_s(self) -> float:
        infl = self.inflight
        if infl and infl["first"]:
            span = time.time() - infl["first"]
            return round(infl["chunks"] / span, 1) if span > 0.25 else 0.0
        return 0.0

    def _fresh_throughput(self) -> dict | None:
        """The latest engine `throughput` event (every 5 s — from the text log or
        the JSONL ledger, both shape .throughput), or None when stale. Engine-
        reported aggregate rates are the only legitimate live-rate source for a
        pure observer: per-chunk accounting lives inside the engine, so the
        chunk-based _live_tok_s is a bonus estimate, never the graph feed."""
        tp = self.reqlog.throughput
        if tp and time.time() - self.reqlog.throughput_at < THROUGHPUT_FRESH_S:
            return tp
        return None

    def _extra(self) -> dict:
        """telemetry.Telemetry `extra` hook: the engine's throughput events
        feed the 60-sample history. A zero rate records 0.0 — the charts plot
        it on the y=0 baseline, line continuous; a stale (no fresh event)
        window records None, an honest gap, never a flat lie."""
        live = self._live_tok_s()
        tp = self._fresh_throughput() or {}
        dec = live or tp.get("decode_tok_s")
        return {"tok_s": dec, "prefill_tok_s_mean": tp.get("prefill_tok_s"),
                "slot_tok_s": [dec]}

    # -- payload builders -----------------------------------------------------
    def engine_ok(self) -> bool:
        return bool(self.engine_up)

    def live(self) -> dict:
        tp = self._fresh_throughput()
        queued = int(tp["waiting"]) if tp else 0
        infl = self.inflight
        if infl is None:
            state = "idle"
        elif infl["first"] is None:
            state = "reading"
        else:
            state = "generating"
        return {"state": state, "load": None, "queued": queued,
                "slots": {"total": 1, "active": 1 if infl else 0, "queue_depth": queued},
                "queue": [],
                "prompt_tokens": None,
                "generated": (infl["chunks"] if infl and infl["first"] else None),
                "elapsed_s": round(time.time() - infl["started"], 1) if infl else None,
                "tok_s": (self._live_tok_s() or (tp or {}).get("decode_tok_s")) if state == "generating" else None,
                "prefill_tok_s_mean": (tp or {}).get("prefill_tok_s")}

    def slot_row(self) -> dict:
        """The single /slots row: live in-flight, else the last finished request dimmed."""
        n_ctx = self.reqlog.kv_tokens or 262144
        infl = self.inflight
        if infl:
            gen = infl["chunks"]
            return {"id": 0, "n_ctx": n_ctx, "is_processing": True,
                    "state": "decoding" if infl["first"] else "prefill",
                    "api": infl["api"], "model": self.reqlog.model or self.model_hint,
                    "tok_s": self._live_tok_s(), "generated": gen, "max_tokens": None,
                    "prompt_tokens": None, "prefill_done": 0, "ctx_used": gen,
                    "reused": None, "ttft_s": None, "draft_len": None,
                    "drafts_offered": None, "drafts_accepted": None}
        r = self.reqlog.last_done or {}
        acc, off = r.get("drafts_accepted"), r.get("drafts_offered")
        return {"id": 0, "n_ctx": n_ctx, "is_processing": False, "state": "idle",
                "api": r.get("api"), "model": self.reqlog.model or self.model_hint,
                "tok_s": 0.0, "generated": 0, "max_tokens": None,
                "prompt_tokens": r.get("prompt_tokens"), "prefill_done": 0,
                "ctx_used": (r.get("prompt_tokens") or 0) + (r.get("output_tokens") or 0),
                "reused": r.get("reused"), "ttft_s": r.get("ttft_s"), "draft_len": None,
                "drafts_offered": off, "drafts_accepted": acc}

    def metrics(self) -> dict:
        """The /metrics dict; call only when engine_ok(); the proxy answers 503 otherwise
        so app.js shows Offline (its catch path), never a fake idle engine."""
        best, p85 = best_and_p85(self.reqlog.requests)
        tel = self.telemetry.snapshot()
        kv = self.reqlog.kv_tokens
        engine = {"model": self.reqlog.model or self.model_hint, "context": kv, "max_context": kv}
        return {"engine": engine, "live": self.live(),
                "requests": self.reqlog.requests[:REQUESTS_SHOWN],
                "best": best, "p85": p85,
                "hardware": tel["now"], "hardware_static": tel["static"],
                "history": tel["history"]}
