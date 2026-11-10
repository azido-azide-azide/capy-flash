"""JSONL ingestion for capy-dash — the machine-readable twin of reqlog.py.

nInfer's `--request-log-jsonl FILE` appends one `ninfer_serve_request_log`
JSON object per event (schema-v28 in capy-flash @ 0d15543; field names read
from src/serve/request_log.cpp). This module exposes the same read surface
DashState uses from ReqLog (.requests/.last_done/.throughput/.throughput_at/
.kv_tokens/.model/.engine_state/.n_lines), so server.py can swap it in when
the flag is on. The live engine does NOT pass the flag — text parsing
(reqlog.py) is the shipped path; this reader is coded for the day the flag
is added to the launch line, and its throughput-event mapping is deliberately
defensive (.get everywhere) until that day verifies the nested names.
"""
from __future__ import annotations

import time

from reqlog import API_MAP, API_PATH

ARTIFACT = "ninfer_serve_request_log"
SCHEMA_MIN = 27

# engine finish_reason names → the dashboard's finish column (mirrors reqlog.FINISH_MAP)
FINISH_MAP = {"stop_token": "stop", "stop_string": "stop", "output_limit": "length",
              "context_capacity": "context capacity", "cancelled": "cancelled",
              "none": "stop", "unknown": "stop"}


class JsonlIngest:
    """Consume event records; shape them like ReqLog does. Never raises."""

    MAX_KEEP = 200

    def __init__(self):
        self.requests: list[dict] = []            # newest first, dashboard-shaped
        self.inflight: dict = {}                  # (instance,id) -> request fields
        self.last_done: dict | None = None
        self.throughput: dict | None = None
        self.throughput_at: float = 0.0
        self.kv_tokens: int | None = None
        self.model: str | None = None
        self.engine_state = "unknown"
        self.n_lines = 0
        self.bad_lines = 0
        self.sessions: dict[str, int] = {}        # server_instance_id -> session no.

    def _session(self, instance_id: str) -> int:
        s = self.sessions.get(instance_id)
        if s is None:
            s = len(self.sessions)
            self.sessions[instance_id] = s
        return s

    def event(self, rec: dict):
        """One parsed JSON object → (event_type, fields) or None. Same contract as
        ReqLog.feed, so follow._drive_inflight works for both sources."""
        if not isinstance(rec, dict) or rec.get("artifact_type") != ARTIFACT \
                or int(rec.get("schema_version", 0)) < SCHEMA_MIN:
            self.bad_lines += 1
            return None
        self.n_lines += 1
        kind = rec.get("event")
        ts = (rec.get("timestamp_unix_ms") or 0) / 1000.0 or time.time()
        sess = self._session(rec.get("server_instance_id", "?"))
        req = rec.get("request") or {}
        rid = req.get("request_id")
        key = (sess, rid)

        if kind == "request_start":
            self.inflight[key] = {"api": req.get("protocol", ""), "session": sess,
                                  "id": rid, "started": ts}
            return ("started", {"api": req.get("protocol", "")})

        if kind in ("request_done", "request_error", "request_rejected"):
            self.inflight.pop(key, None)
            if kind != "request_done":
                return ("failed", {"id": rid})
            done = self._done_fields(req, rec, ts, sess)
            self.requests.insert(0, done)
            del self.requests[self.MAX_KEEP:]
            self.last_done = done
            return ("done", done)

        if kind == "throughput":
            self.throughput = self._throughput_fields(rec)
            self.throughput_at = ts
            return ("throughput", {"run_detail": "decode-ready"
                                   if rec.get("decode_ready") else ""})

        if kind == "server_start":
            self.model = rec.get("model") or self.model
            cap = rec.get("context_capacity") or rec.get("kv_capacity")
            if isinstance(cap, dict):
                cap = cap.get("tokens") or cap.get("context_tokens")
            if isinstance(cap, int):
                self.kv_tokens = cap
            self.engine_state = "up"
            return ("listening", {"model": self.model})

        return ("other", {"msg": kind or "?"})

    # -- shaping ------------------------------------------------------------
    @staticmethod
    def _done_fields(req: dict, rec: dict, ts: float, sess: int) -> dict:
        res = rec.get("result") or {}
        tim = rec.get("timings_seconds") or {}
        spec = rec.get("speculative") or {}
        prompt = res.get("prompt_tokens")
        out = res.get("completion_tokens")
        hit = res.get("prefix_cache_hit_tokens") or 0
        computed = res.get("computed_prefill_tokens")
        if computed is None and prompt is not None:
            computed = prompt - hit
        prefill_s, decode_s, ttft_s = tim.get("prefill"), tim.get("decode"), tim.get("ttft")
        proto = req.get("protocol", "")
        finish = res.get("finish_reason") or "none"
        return {"id": f"req#{req.get('request_id')}", "session": sess, "time": ts, "slot": 0,
                "path": API_PATH.get(proto, proto), "api": API_MAP.get(proto, proto),
                "output_tokens": out,
                "decode_tok_s": round(out / decode_s, 1) if out and decode_s else None,
                "duration_s": round(tim["total"], 2) if tim.get("total") else None,
                "finish": FINISH_MAP.get(finish, finish),
                "prompt_tokens": prompt, "reused": hit,
                "prompt_ms": round(prefill_s * 1000, 1) if prefill_s else None,
                "ttft_s": round(ttft_s, 3) if ttft_s else None,
                "queue_s": None,                     # JSONL carries prepare (not queue); stay honest
                "cache_pct": round(hit / prompt * 100, 1) if prompt else None,
                "cache_path": res.get("prefix_reuse_path"),
                "spec_backend": spec.get("backend"),
                "drafts_accepted": spec.get("accepted_tokens"),
                "drafts_offered": spec.get("drafted_tokens"),
                "thinking_tokens": res.get("model_thinking_tokens"),
                "thinking_budget": res.get("thinking_budget")}

    @staticmethod
    def _throughput_fields(rec: dict) -> dict:
        """Counters + elapsed_seconds → the ReqLog-shaped rates (best-effort until
        the flag goes live; unknown nested names stay None, never guessed)."""
        el = rec.get("elapsed_seconds")
        pre = rec.get("prefill")
        dec = rec.get("decode")
        batch = rec.get("decode_batch") or {}
        return {"decode_tok_s": round(dec / el, 1) if dec and el else None,
                "prefill_tok_s": round(pre / el, 1) if pre and el else None,
                "running": int(rec.get("running") or 0),
                "waiting": int(rec.get("waiting") or 0),
                "batch": batch.get("average_size") if isinstance(batch, dict) else None}
