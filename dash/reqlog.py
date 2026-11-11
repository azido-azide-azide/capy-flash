"""Fail-soft parser for ninfer-serve operational log lines (src/serve/operational_log.cpp).

Every line arrives wrapped in the spdlog envelope:

    2026-11-09 13:11:50.044  INFO  req#17 done | openai-chat | ...
    ^date       ^time.^ms     ^level ^message

Grammar documented from the C++ renderers (see samples/operational_log.cpp.remote):
  - counts are thousands-grouped ("51,220")            -> format_pretty_count
  - durations: "900 us" | "28.0 ms" | "48.5s" | "4m 2.9s" | "1h 5m"   format_pretty_duration
  - percents: "84.7%" ("n/a" when not finite)                          format_pretty_percent
  - rates: "2.55k tok/s" (SI prefixes k M G T; 1 decimal below 1000)   format_pretty_rate

FAIL-SOFT RULE: a line whose message matches no known renderer is still a valid
log line - it goes to the ring at its envelope level and structured state simply
does not move. Panels fall back to their last known value (stale) and the
frontend null-tolerates "-" everywhere. Nothing here ever raises on bad input.

req# ids restart at 1 on every server session; a `listening on` line starts a
new session so id reuse across restarts cannot mix request state.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

# ---------------------------------------------------------------- envelope
ENVELOPE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2})\.(?P<ms>\d{3})\s+"
    r"(?P<level>TRACE|DEBUG|INFO|WARNING|WARN|ERROR|ERR|FATAL|CRITICAL)\s+(?P<msg>.*)$")

LEVEL_MAP = {"TRACE": "info", "DEBUG": "info", "INFO": "info",
             "WARN": "warn", "WARNING": "warn",
             "ERROR": "err", "ERR": "err", "FATAL": "err", "CRITICAL": "err"}

# ---------------------------------------------------------------- clause atoms
CNT = r"[\d,]+"
DUR = r"(?:\d+h \d+m|\d+m [\d.]+s|[\d.]+ ms|[\d.]+ us|[\d.]+s)"
RATE = r"[\d.]+[kMGT]? tok/s"
PCT = r"(?:[\d.]+|n/a)%"
API = r"[\w-]+"                      # openai-chat | openai-responses | anthropic | ...
PHASE = r"(?:prepare|generation|response rendering|response storage|transport|http)"

# ---------------------------------------------------------------- message renderers
STARTED = re.compile(
    rf"^req#(?P<id>\d+) started \| (?P<api>{API}) (?P<mode>stream|non-stream) \| "
    rf"(?P<messages>{CNT}) messages? \| max output (?P<max_output>{CNT}) \| thinking (?P<thinking>[^|]+?)"
    rf"(?: \|(?: media {CNT}(?:, prepared [^|]+)?| tools (?P<tools>{CNT})| preserve thinking))*$")

DONE = re.compile(
    rf"^req#(?P<id>\d+) done \| (?P<api>{API}) \| (?P<finish>tool calls {CNT}|[^|]+?) \| "
    rf"prompt (?P<prompt>{CNT}) \| output (?P<output>{CNT}) \| cache (?P<cache>{CNT}) "
    rf"\((?P<cache_pct>{PCT})(?:, (?P<cache_path>[^)]+))?\) \| TTFT (?P<ttft>{DUR}) \| total (?P<total>{DUR})"
    rf"(?: \| queue (?P<queue>{DUR}))?(?: \| prefill (?P<prefill_rate>{RATE}))?(?: \| decode (?P<decode_rate>{RATE}))?"
    rf"(?: \| (?P<spec_backend>[\w-]+) accepted (?P<spec_acc>{CNT})/(?P<spec_draft>{CNT}) \((?P<spec_pct>{PCT})\))?"
    rf"(?: \| thinking (?P<think_tokens>{CNT})/(?P<think_budget>{CNT})(?:, control (?P<think_control>{CNT}))?)?$")

REJECTED = re.compile(                       # status: rejected | cancelled | failed (during prepare, rejected path)
    rf"^req#(?P<id>\d+) (?P<word>rejected|cancelled|failed) during prepare \| (?P<api>{API}) (?P<mode>stream|non-stream)"
    rf"(?: \| HTTP (?P<http>\d{{3}}))?(?: \| (?P<cause>[^|]+?))?(?: \| messages {CNT})?(?: \| media {CNT})?(?: \| tools {CNT})?$")

FAILED = re.compile(                         # failed|cancelled during <phase>, plus protocol
    rf"^req#(?P<id>\d+) (?P<word>failed|cancelled) during (?P<phase>{PHASE}) \| (?P<api>{API})"
    rf"(?: \| HTTP (?P<http>\d{{3}}))?(?: \| (?P<cause>.+))?$")

RESPONSE_FAILED = re.compile(
    rf"^req#(?P<id>\d+) response failed during (?P<phase>{PHASE})"
    rf"(?: \| HTTP (?P<http>\d{{3}}))?(?: \| (?P<cause>.+))?$")

PREFILL_MISS = re.compile(
    rf"^req#(?P<id>\d+) prefill miss \| recomputed (?P<recomputed>{CNT}) of (?P<prompt>{CNT}) \| "
    rf"reused (?P<reused>{CNT}) \| common prefix (?P<common>{CNT}) with a recent context of (?P<old_ctx>{CNT}) "
    rf"\| (?P<dump>dump failed|dump .*)$")

TOOL_MARKUP = re.compile(rf"^req#(?P<id>\d+) tool markup returned as text \| (?P<reason>.+)$")

THROUGHPUT = re.compile(
    rf"^throughput \| (?P<interval>{DUR})(?: \| prefill (?P<prefill>{RATE}) \((?P<prefill_tok>{CNT}) tok\))?"
    rf"(?: \| decode (?P<decode>{RATE}) \((?P<decode_tok>{CNT}) tok\))? \| running (?P<running>\d+)"
    rf"(?: \((?P<run_detail>[^)]*)\))?(?: \| waiting (?P<waiting>\d+))?(?: \| materializing \d+)?"
    rf"(?: \| capture-pending \d+)?(?: \| terminal-pending \d+)?(?: \| batch (?P<batch>[\d.]+))?"
    rf"(?: \| host (?P<host>{PCT}) \({DUR}\))$")

CAPACITY = re.compile(
    rf"^capacity \| KV (?P<kv_tokens>{CNT}) tokens, (?P<kv_dtype>\w+), (?P<kv_mode>auto|explicit) \| "
    rf"pages {CNT}/{CNT} \| runtime [^|]+ \| free .*$")

LISTENING = re.compile(
    r"^listening on http://(?P<host>[^:]+):(?P<port>\d+) \| model (?P<model>\S+) \| auth (?P<auth>bearer|disabled)$")

SERVER_FAILED = re.compile(r"^server failed during (?P<stage>startup|serving) \| (?P<detail>.+)$")
SERVER_STOPPED = re.compile(r"^server stopped$")
WARMUP = re.compile(rf"^warmup complete \| {DUR}$")
ENGINE_UNAVAILABLE = re.compile(r"^engine unavailable after an engine-wide failure")
CANNOT_BIND = re.compile(r"^cannot bind (?P<host>\S+):(?P<port>\d+)")
HTTP_FAILED = re.compile(
    rf"^HTTP request failed during (?P<phase>.+?) \| (?P<api>{API})(?: \| request .*?)?"
    rf"(?: \| HTTP (?P<http>\d{{3}}))?(?: \| (?P<cause>.+))?$")

FINISH_MAP = {"stop token": "stop", "stop string": "stop", "output limit": "length",
              "context capacity": "context capacity", "cancelled": "cancelled", "none": "stop"}
API_MAP = {"openai-chat": "openai", "openai-responses": "openai", "anthropic": "anthropic",
           "openai-input-tokens": "openai", "anthropic-count-tokens": "anthropic"}
API_PATH = {"openai-chat": "/v1/chat/completions", "openai-responses": "/v1/responses",
            "anthropic": "/v1/messages"}

_K = {"": 1.0, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}


# ---------------------------------------------------------------- value parsers (never raise)
def pcount(s):
    """'51,220' -> 51220; None on garbage."""
    try:
        return int(s.replace(",", ""))
    except (AttributeError, ValueError):
        return None


def pdur(s):
    """DUR -> seconds (float); None on garbage."""
    if not s:
        return None
    s = s.strip()
    try:
        if s.endswith("us"):
            return float(s[:-2]) * 1e-6
        if s.endswith("ms"):
            return float(s[:-3]) * 1e-3
        m = re.fullmatch(r"(?P<h>\d+)h (?P<m>\d+)m", s)
        if m:
            return int(m["h"]) * 3600 + int(m["m"]) * 60
        m = re.fullmatch(r"(?P<m>\d+)m (?P<s>[\d.]+)s", s)
        if m:
            return int(m["m"]) * 60 + float(m["s"])
        if s.endswith("s"):
            return float(s[:-1])
    except ValueError:
        return None
    return None


def prate(s):
    """'2.55k tok/s' -> 2550.0; None on garbage."""
    if not s:
        return None
    m = re.fullmatch(r"([\d.]+)([kMGT]?) tok/s", s.strip())
    if not m:
        return None
    return float(m[1]) * _K[m[2]]


def ppct(s):
    """'84.7%' -> 0.847; 'n/a%' -> None."""
    if not s:
        return None
    m = re.fullmatch(r"([\d.]+)%", s.strip())
    return float(m[1]) / 100.0 if m else None


def epoch(date_s, time_s, ms_s):
    try:
        dt = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M:%S")
        return round(dt.timestamp() + int(ms_s) / 1000.0, 3)
    except (ValueError, OSError):
        return round(time.time(), 3)


class NinferLevelReader:
    """loglevel-compatible: the level is the envelope token, no message searching."""

    def __init__(self):
        self._fallback = None

    def read(self, line: str) -> str:
        m = ENVELOPE.match(line)
        return LEVEL_MAP.get(m["level"], "info") if m else "info"


# ---------------------------------------------------------------- structured state
class ReqLog:
    """Feed envelope lines in; read structured dash state out. Never raises."""

    MAX_KEEP = 200

    def __init__(self):
        self.session = 0
        self.requests: list[dict] = []            # newest first, dashboard-shaped
        self.inflight: dict = {}                  # (session,id) -> started info
        self.last_done: dict | None = None        # last finished request, for the idle slot row
        self.throughput: dict | None = None       # last `throughput` line
        self.throughput_at: float = 0.0
        self.kv_tokens: int | None = None         # from `capacity`
        self.model: str | None = None             # from `listening on`
        self.engine_state = "unknown"             # up | down | unknown
        self.n_lines = 0

    # -- ingestion ----------------------------------------------------------
    def feed(self, line: str):
        """Parse one raw log line; update state; return (event_type, fields) or None."""
        self.n_lines += 1
        try:
            return self._feed(line)
        except Exception:                          # fail-soft: a bad line never takes the dash down
            return None

    def _feed(self, line: str):
        m = ENVELOPE.match(line)
        if not m:                                  # not ninfer's format: ignore structurally
            return None
        lvl, msg, t = LEVEL_MAP.get(m["level"], "info"), m["msg"], epoch(m["date"], m["time"], m["ms"])

        g = LISTENING.match(msg)
        if g:
            self.session += 1
            self.inflight.clear()          # a new server process: everything older is dead
            self.model, self.engine_state = g["model"], "up"
            return ("listening", g.groupdict())

        if SERVER_STOPPED.match(msg):
            self.engine_state = "down"
            self.inflight.clear()
            return ("server_stopped", {})

        g = SERVER_FAILED.match(msg)
        if g:
            self.engine_state = "down"
            self.inflight.clear()
            return ("server_failed", g.groupdict())

        if ENGINE_UNAVAILABLE.match(msg):
            self.engine_state = "down"
            return ("engine_unavailable", {})

        g = STARTED.match(msg)
        if g:
            key = (self.session, int(g["id"]))
            self.inflight[key] = {"api": g["api"], "mode": g["mode"], "started": t,
                                  "messages": pcount(g["messages"]), "max_output": pcount(g["max_output"])}
            return ("started", g.groupdict())

        g = DONE.match(msg)
        if g:
            key = (self.session, int(g["id"]))
            start = self.inflight.pop(key, None)
            req = self._finish_fields(g, t, start)
            self.requests.insert(0, req)
            del self.requests[self.MAX_KEEP:]
            self.last_done = req
            return ("done", req)

        g = REJECTED.match(msg) or FAILED.match(msg) or RESPONSE_FAILED.match(msg)
        if g:
            self.inflight.pop((self.session, int(g["id"])), None)
            # finished-but-not-ok requests stay out of the requests table (basalt lists
            # completed generations there); the line itself reaches the log ring at WARN.
            return ("failed", g.groupdict())

        g = PREFILL_MISS.match(msg)
        if g:
            return ("prefill_miss", g.groupdict())

        g = TOOL_MARKUP.match(msg)
        if g:
            return ("tool_markup", g.groupdict())

        g = THROUGHPUT.match(msg)
        if g:
            # an absent half means zero tokens of that class in the window:
            # record 0.0 (the chart plots it on the y=0 baseline)
            self.throughput = {
                "decode_tok_s": prate(g["decode"]) or 0.0,
                "prefill_tok_s": prate(g["prefill"]) or 0.0,
                "running": int(g["running"]), "waiting": int(g["waiting"] or 0),
                "batch": float(g["batch"]) if g["batch"] else None}
            self.throughput_at = t
            return ("throughput", g.groupdict())

        g = CAPACITY.match(msg)
        if g:
            self.kv_tokens = pcount(g["kv_tokens"])
            return ("capacity", g.groupdict())

        if WARMUP.match(msg):
            return ("warmup", {})
        if CANNOT_BIND.match(msg):
            return ("cannot_bind", {})
        g = HTTP_FAILED.match(msg)
        if g:
            return ("http_failed", g.groupdict())
        return ("other", {"lvl": lvl, "msg": msg})       # unknown renderer: kept only in the ring

    # -- shaping for the dashboard -----------------------------------------
    def _finish_fields(self, g, t: float, start: dict | None) -> dict:
        prompt, out, cache = pcount(g["prompt"]), pcount(g["output"]), pcount(g["cache"])
        pre_rate = prate(g["prefill_rate"])
        read = (prompt or 0) - (cache or 0)
        prompt_ms = round(read / pre_rate * 1000, 1) if pre_rate and read > 0 else None
        finish = g["finish"] or ""
        if finish.startswith("tool calls"):
            finish = "stop"
        else:
            finish = FINISH_MAP.get(finish.strip(), finish.strip() or "stop")
        api = API_MAP.get(g["api"], g["api"])
        return {"id": f"req#{g['id']}", "session": self.session, "time": t, "slot": 0,
                "path": API_PATH.get(g["api"], g["api"]), "api": api,
                "output_tokens": out, "decode_tok_s": round(prate(g["decode_rate"]), 1) if prate(g["decode_rate"]) else None,
                "duration_s": round(pdur(g["total"]), 2) if pdur(g["total"]) else None,
                "finish": finish, "prompt_tokens": prompt, "reused": cache, "prompt_ms": prompt_ms,
                "ttft_s": round(pdur(g["ttft"]), 3) if pdur(g["ttft"]) else None,
                "queue_s": round(pdur(g["queue"]), 3) if pdur(g["queue"]) else None,
                "cache_pct": ppct(g["cache_pct"]), "cache_path": g["cache_path"],
                "spec_backend": g["spec_backend"],
                "drafts_accepted": pcount(g["spec_acc"]), "drafts_offered": pcount(g["spec_draft"]),
                "thinking_tokens": pcount(g["think_tokens"]), "thinking_budget": pcount(g["think_budget"])}


class LogRing:
    """logbuf.LogBuffer fed with envelope-timestamped lines (so seeded history shows real times).

    Thin wrapper: keeps basalt's LogBuffer (ids, levels counts, level_lines, since)
    but replaces `t: now` with the line's parsed envelope time when present.
    """

    def __init__(self, buffer):
        self.buffer = buffer                          # a patched logbuf.LogBuffer

    def add_line(self, raw: str, src: str = "eng") -> None:
        raw = raw.rstrip("\r\n")
        if not raw.strip():
            return
        m = ENVELOPE.match(raw)
        with self.buffer.lock:
            before = self.buffer.last
        self.buffer.add(src, raw)
        if m:                                          # stamp the just-added entries with the engine's clock
            t = epoch(m["date"], m["time"], m["ms"])
            with self.buffer.lock:
                for entry in self.buffer.lines:
                    if entry["id"] > before:
                        entry["t"] = t

    def seed_file(self, path: str, tail: int = 500) -> int:
        """Load the newest `tail` lines of a (possibly absent) log file; returns count added."""
        try:
            with open(path, "rb") as f:
                data = f.read()[-262144:]                      # last 256 KiB is plenty
            lines = data.decode("utf-8", "replace").splitlines()[-tail:]
        except OSError:
            return 0
        for line in lines:
            self.add_line(line)
        return len(lines)
