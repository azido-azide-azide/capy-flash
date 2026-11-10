# capy-dash — monitoring dashboard for `ninfer-serve`

A web dashboard (hero rates, request history, best/p85, single-slot row, log
panel with level tabs, GPU/CPU/RAM telemetry) that observes a running
[nInfer](https://github.com/azido-azide-azide/capy-flash) `ninfer-serve`
process **from the outside**:

* tails the operational log (`--log`, default `/var/log/ninfer-serve.log`)
  and parses the throughput / `req#N started|done` renderers into history,
* polls the engine's own `GET /health` and `GET /v1/models` for liveness and
  model identity,
* serves the UI plus `/metrics`, `/slots`, `/api/logs` on its own port.

It never supervises, proxies, signals or restarts the engine — there is no hop
in front of `/v1`. Zero serving-path risk is the whole point: this sidecar can
be started, killed, or broken without the engine noticing.

## Run

```
python3 dash/server.py --listen 0.0.0.0:8098 --engine 127.0.0.1:8000 \
    --log /var/log/ninfer-serve.log
```

Stdlib only (Python ≥ 3.10; `psutil` is used opportunistically by
`telemetry.py` when present, `/proc` is the fallback). `--api-key` guards the
dash endpoints only. `--request-log-jsonl FILE` switches the per-request
ingestion from log parsing to the engine's structured
`ninfer_serve_request_log` (see *JSONL mode* below) — it requires the matching
engine launch flag and is **off by default**, matching the live launch line.

Tests: `python3 dash/tests/test_reqlog.py`, `test_logbuf.py`,
`test_metrics.py`, `test_follow.py` (plain stdlib `unittest`). They replay the
real log excerpts under `dash/fixtures/`.

## Provenance

The dashboard is **vendored from the `serve/` subtree of
`azido-azide-azide/basalt` @ `cc0a7636900a4e528ddb815439b8b83126a4551e`**
(MIT, Jesús Gulfo; the repo this lives in is Apache-2.0 — the MIT attribution
travels with the vendored files listed below):

| file | basalt origin | change |
|---|---|---|
| `web/{index.html,app.js,app.css,mark.svg}` | `serve/web/*` | trimmed: experts panel, model picker, load bar, font preloads removed; single-slot mode kept |
| `logbuf.py` | `serve/util/logbuf.py` | one patch: `level_reader_factory` (ninfer levels come from the spdlog envelope, not basalt markers) |
| `loglevel.py` | `serve/util/loglevel.py` | as-is (generic level model) |
| `telemetry.py` | `serve/util/telemetry.py` | as-is (NVML via ctypes, psutil/`/proc`) |
| `metrics.py` | `serve/service.py` | `best_and_p85` gates + nearest-rank ported verbatim; `DashState` re-sources the same payload from log-parsed state |
| `server.py` routes | `serve/handler.py` | `/`, `/web/*`, `/metrics`, `/slots`, `/api/logs`, `/health` ported; no `/v1`, no forwarding |

New here (no basalt origin): `reqlog.py` (the ninfer operational-log parser:
envelope levels, `req#N` lifecycle, throughput lines, value parsers for
`43,158` / `1.03k tok/s` / `2.5s` / cache annotations), `follow.py` (tail +
poll threads), `jsonl.py` (JSONL twin of the parser), `server.py` (main).

## `/metrics` schema contract

The key set mirrors basalt `serve/service.py` `metrics()` @ `cc0a763` so the
vendored `app.js` renders it unchanged. **Changing any key set below requires
updating `tests/test_metrics.py` deliberately — the test asserts these exact
sets** (`swap`/`models` from basalt are deliberately absent: single engine,
single model).

```
{
  "engine":   {"model": str, "context": int, "max_context": int},
  "live":     {"state": "idle"|"reading"|"generating", "load": None, "queued": int,
               "slots": {"total": 1, "active": 0|1, "queue_depth": int}, "queue": [],
               "prompt_tokens": None, "generated": int|None, "elapsed_s": float|None,
               "tok_s": float|None, "prefill_tok_s_mean": None},
  "requests": [ {"id": "req#N", "session": int, "time": float(unix), "slot": 0,
                 "path": str, "api": "openai"|"anthropic"|str,
                 "output_tokens": int, "decode_tok_s": float|None,
                 "duration_s": float|None,
                 "finish": "stop"|"length"|"context capacity"|"cancelled",
                 "prompt_tokens": int, "reused": int, "prompt_ms": float|None,
                 "ttft_s": float|None, "queue_s": float|None,
                 "cache_pct": float|None, "cache_path": str|None,
                 "spec_backend": str|None, "drafts_accepted": int|None,
                 "drafts_offered": int|None, "thinking_tokens": int|None,
                 "thinking_budget": int|None}, ... ],   # newest first, ≤ 12
  "best":     {"decode": {"tok_s","id","time","output_tokens"},
               "prefill": {"tok_s","id","time","read_tokens"}},   # gated: out≥256, read≥4096
  "p85":      {"decode"|"prefill": {"tok_s": float, "n": int, "min_tokens": int}},
  "hardware": {...}, "hardware_static": {...}, "history": {...}    # telemetry.py, as vendored
}
```

Companion routes: `GET /slots` → one-row list (basalt `slot_table()[0]` shape;
idle row = last finished request dimmed). `GET /api/logs?after=&limit=&level=`
→ `{"lines":[{"id","t","src","lvl","msg"}], "last": int, "levels": {...}}`.
`/metrics` and `/slots` answer **503 while the engine's `/health` is down** so
the UI shows *Offline* rather than a fake idle engine. `GET /health` is the
dash's own liveness (`{"status","service":"capy-dash","engine_up","api_key","model"}`).

History semantics: request history and log ring live inside the sidecar and
survive engine restarts (the log is append-mode across sessions); a sidecar
restart re-seeds from the last 500 log lines.

## JSONL mode (coded, OFF)

`ninfer-serve --request-log-jsonl FILE` emits `ninfer_serve_request_log`
schema-v28 JSONL (`src/serve/request_log.cpp`) with exact per-request
numbers. `jsonl.py` + `--request-log-jsonl` consume it and replace the regex
parser for history state. Enabling it end-to-end means adding the launch flag
to the engine start path — an engine restart decision that is explicitly out
of scope here; keep the flag off to match the live launch.
