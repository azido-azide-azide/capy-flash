"""capy-dash: the basalt monitoring dashboard, observing nInfer capy-flash.

Mode B (observer sidecar): tails /var/log/ninfer-serve.log, polls the engine's
/health and /v1/models, and serves the ported dashboard + /metrics + /slots +
/api/logs on its own port. It never touches the engine process: no signals,
no supervision, no hop in front of the API. See dash/README.md for provenance
and the /metrics schema contract.

Run:  python3 server.py [--listen 0.0.0.0:8098] [--engine 127.0.0.1:8000]
        [--log /var/log/ninfer-serve.log] [--request-log-jsonl FILE]
        [--api-key KEY] [--gpu 0] [--gpu 1]
"""
from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from follow import poll_engine, tail_jsonl, tail_log
from jsonl import JsonlIngest
from metrics import DashState

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STATE: DashState                      # set in main()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "capy-dash/0.1"

    # ---------------------------------------------------------------- plumbing
    def log_message(self, format, *args):    # quiet; our own ring gets what matters
        pass

    def _json(self, code: int, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not STATE.api_key:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {STATE.api_key}"

    # ---------------------------------------------------------------- dashboard
    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        query = urlsplit(self.path).query
        if path in ("/", "") or path.startswith("/web/"):
            return self._static(path)
        if path.startswith("/fonts/"):
            return self._fonts(path)
        if path in ("/health", "/api/health"):
            return self._json(200, {"status": "ok", "service": "capy-dash",
                                    "engine_up": bool(STATE.engine_up),
                                    "api_key": bool(STATE.api_key),
                                    "model": STATE.reqlog.model or STATE.model_hint})
        if not self._authorized():
            return self._json(401, {"error": {"message": "missing or wrong API key"}})
        if path == "/metrics":
            if not STATE.engine_ok():
                return self._json(503, {"error": {"message": "engine unavailable"}})
            return self._json(200, STATE.metrics())
        if path == "/slots":
            if not STATE.engine_ok():
                return self._json(503, {"error": {"message": "engine unavailable"}})
            return self._json(200, [STATE.slot_row()])
        if path == "/api/logs":
            return self._logs(query)
        return self._json(404, {"error": "not found"})

    def _static(self, path: str):
        if path in ("", "/"):
            return self._file(os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8")
        name = path[len("/web/"):]                     # flat files only, no traversal
        if not name or "/" in name or name.startswith("."):
            return self._json(404, {"error": "not found"})
        ctype = ("text/css" if name.endswith(".css") else
                 "text/javascript" if name.endswith(".js") else
                 "image/svg+xml" if name.endswith(".svg") else
                 "font/woff2" if name.endswith(".woff2") else "application/octet-stream")
        return self._file(os.path.join(WEB_DIR, name), ctype)

    def _fonts(self, path: str):
        name = path[len("/fonts/"):]                   # flat .woff2 only (basalt handler parity)
        if not name or "/" in name or "\\" in name or not name.endswith(".woff2"):
            return self._json(404, {"error": {"message": "not found"}})
        return self._file(os.path.join(WEB_DIR, "fonts", name), "font/woff2")

    def _logs(self, query: str):
        from urllib.parse import parse_qs
        q = parse_qs(query)

        def number(name, default):
            try:
                return max(0, int(q.get(name, [default])[0]))
            except (ValueError, IndexError):
                return default
        after = number("after", 0)
        limit = min(number("limit", 500), 2000)
        level = (q.get("level", [""])[0]).lower()
        buf = STATE.ring.buffer
        if level in ("info", "warn", "err"):
            lines, last = buf.level_lines(level, after, limit)      # the p85-era fix, ported
        else:
            lines, last = buf.since(after, limit)
        return self._json(200, {"lines": lines, "last": last, "levels": buf.levels()})

    def _file(self, full: str, ctype: str):
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            return self._json(404, {"error": "not found"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def seed(state: DashState, log_path: str, tail: int = 500) -> int:
    """Load the newest `tail` log lines into the ring (log panel) and the parser
    (history) so the dashboard opens with context. Returns the line count seeded."""
    try:
        with open(log_path, "rb") as f:
            data = f.read()[-262144:]
        lines = data.decode("utf-8", "replace").splitlines()[-tail:]
    except OSError:
        return 0
    for line in lines:
        state.ring.add_line(line)
        state.reqlog.feed(line)
    return len(lines)


def main():
    global STATE
    ap = argparse.ArgumentParser(description="capy-flash monitoring dashboard (observer sidecar)")
    ap.add_argument("--listen", default="0.0.0.0:8098")
    ap.add_argument("--engine", default="127.0.0.1:8000", help="nInfer host:port (GET /health, /v1/models only)")
    ap.add_argument("--log", default="/var/log/ninfer-serve.log")
    ap.add_argument("--request-log-jsonl", default="",
                    help="tail this JSONL (ninfer_serve_request_log) for exact per-request "
                         "numbers INSTEAD of parsing --log; the live engine (CT131) passes "
                         "--request-log-jsonl and the deployed unit does the same")
    ap.add_argument("--api-key", default="", help="guards the dash endpoints only; /v1 auth stays at the engine")
    ap.add_argument("--gpu", action="append", type=int, default=None)
    args = ap.parse_args()

    lhost, lport = args.listen.rsplit(":", 1)
    ehost, eport = args.engine.rsplit(":", 1)
    STATE = DashState(args.log, gpu_indices=args.gpu or [0, 1], api_key=args.api_key)

    json_mode = bool(args.request_log_jsonl)
    if json_mode:
        ingest = JsonlIngest()
        STATE.reqlog = ingest
        if os.path.exists(args.request_log_jsonl):
            with open(args.request_log_jsonl, "rb") as f:
                for raw in f.read()[-262144:].decode("utf-8", "replace").splitlines()[-500:]:
                    try:
                        ingest.event(json.loads(raw))
                    except ValueError:
                        ingest.bad_lines += 1
        STATE.ring.seed_file(args.log, tail=500)       # log panel only; parser from JSONL
    else:
        seed(STATE, args.log)

    threading.Thread(target=tail_log, daemon=True,
                     args=(STATE, args.log),
                     kwargs={"drive_inflight": not json_mode, "parse": not json_mode}).start()
    if json_mode:
        threading.Thread(target=tail_jsonl, daemon=True,
                         args=(STATE, args.request_log_jsonl, STATE.reqlog)).start()
    threading.Thread(target=poll_engine, daemon=True,
                     args=(STATE, ehost, int(eport))).start()

    srv = ThreadingHTTPServer((lhost, int(lport)), Handler)
    srv.daemon_threads = True
    STATE.ring.add_line(f"[capy-dash] listening on http://{lhost}:{lport} | engine "
                        f"http://{ehost}:{eport} | log {args.log}"
                        + (f" | jsonl {args.request_log_jsonl}" if json_mode else ""), src="srv")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
