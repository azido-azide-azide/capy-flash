"""Ingestion threads for capy-dash: log tail, engine poller, JSONL tail.

The tail mechanics mirror basalt serve/util/logbuf.py `tail_file` (fork
azido-azide-azide/basalt @ cc0a7636, MIT — see dash/README.md); driving the
dashboard's in-flight view from engine-log events and the JSONL follower are
new here. Nothing in this module ever touches the engine process — it only
reads files and sends two GETs per poll cycle to the engine's own HTTP port.
"""
from __future__ import annotations

import json
import os
import socket
import time
from http.client import HTTPConnection

from reqlog import API_MAP, API_PATH


def _pump(path: str, poll_s: float, on_line) -> None:
    """Follow `path` from its current end, calling on_line(text) per complete line.
    A size shrink (rotation, manual trim) restarts from 0; a missing file is
    waited for. Never raises, never dies."""
    try:
        pos = os.path.getsize(path)
    except OSError:
        pos = 0
    partial = b""
    while True:
        try:
            size = os.path.getsize(path)
            if size < pos:                          # defensive: shrink = start over
                pos, partial = 0, b""
            if size > pos:
                with open(path, "rb") as f:
                    f.seek(pos)
                    data = f.read(min(size - pos, 1 << 20))
                pos += len(data)
                *done, partial = (partial + data).split(b"\n")
                for raw in done:
                    on_line(raw.decode("utf-8", "replace"))
                continue
        except Exception:                           # fail-soft: a lost log file only costs log lines
            pass
        time.sleep(poll_s)


def _drive_inflight(state, kind: str, fields: dict) -> None:
    """Mirror the engine's request lifecycle into DashState.inflight so live()/
    slot_row() show reading→generating without sitting in front of the socket.
    Evidence: `req#N started` opens the view; a throughput line carrying
    `decode-ready` marks generation started; done/failed/server_stopped close it."""
    if kind == "started":
        api = fields.get("api", "")
        state.begin_request(API_PATH.get(api, api), API_MAP.get(api, api))
    elif kind in ("done", "failed", "server_stopped", "server_failed"):
        state.end_request()
    elif kind == "throughput" and state.inflight is not None and state.inflight["first"] is None:
        detail = fields.get("run_detail") or ""
        if "decode-ready" in detail:
            state.inflight["first"] = time.time()


def tail_log(state, path: str, poll_s: float = 0.5,
             drive_inflight: bool = True, parse: bool = True) -> None:
    """Follow the operational log: every line feeds the ring (log panel); with
    parse=True the same line feeds the structured parser. When a JSONL feed owns
    the structured state, pass parse=False and the log stays panel-only."""
    def on_line(text: str):
        state.ring.add_line(text)
        ev = state.reqlog.feed(text) if parse else None
        if ev and drive_inflight:
            _drive_inflight(state, *ev)
    _pump(path, poll_s, on_line)


def poll_engine(state, host: str, port: int, every_s: float = 5.0) -> None:
    """Remember whether the engine answers /health (and its model id via
    /v1/models). Only decides whether /metrics answers or says 503 — the
    dashboard never holds the engine's requests."""
    while True:
        up, model = False, None
        try:
            c = HTTPConnection(host, port, timeout=3)
            c.request("GET", "/health")
            r = c.getresponse()
            up = r.status == 200
            r.read()
            c.close()
            if up:
                c = HTTPConnection(host, port, timeout=3)
                c.request("GET", "/v1/models")
                r = c.getresponse()
                if r.status == 200:
                    try:
                        data = json.loads(r.read().decode())
                        models = data.get("data") or data.get("models") or []
                        if models:
                            model = models[0].get("id")
                    except (ValueError, AttributeError):
                        pass
                c.close()
        except (OSError, socket.timeout):
            pass
        state.engine_up = up
        if model and not state.reqlog.model:
            state.model_hint = model
        time.sleep(every_s)


def tail_jsonl(state, path: str, ingest, poll_s: float = 1.0,
               drive_inflight: bool = True) -> None:
    """Follow the engine's --request-log-jsonl file (schema
    `ninfer_serve_request_log`); each JSON line goes to `ingest.event(rec)`.
    Coded-but-off by default: enabling it means adding the launch flag to the
    engine, which is a separate decision (see dash/README.md)."""
    def on_line(text: str):
        text = text.strip()
        if not text:
            return
        try:
            rec = json.loads(text)
        except ValueError:
            return
        state.reqlog.n_lines += 1
        ev = ingest.event(rec)
        if ev and drive_inflight:
            _drive_inflight(state, *ev)
    _pump(path, poll_s, on_line)
