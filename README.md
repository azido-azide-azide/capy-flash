
<h1 align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/logo/capy-flash-lockup-dark.svg">
  <img alt="capy-flash" src="docs/logo/capy-flash-lockup.svg" width="560">
</picture></h1>

## This fork

Upstream, plus the one addition this deployment runs on top of it:

- **`dash/`**: a monitoring dashboard that observes a running `ninfer-serve` purely from the outside — the tail
  of its operational log, `GET /health` and `/v1/models` polls, and optionally the request JSONL ledger
  (`--request-log-jsonl`, which is coded and defaults off). Hero rates, request history, best and p85, a
  single-slot row, a level-tabbed log and GPU/CPU telemetry, on a page that stays up across an engine restart.
  Stdlib only (Python ≥ 3.10; `psutil` used opportunistically by `telemetry.py` when present, `/proc` as the
  fallback), its own process and its own port
  (`python3 dash/server.py --listen 0.0.0.0:8098 --engine 127.0.0.1:8000 --log /var/log/ninfer-serve.log`):
  no supervision, no proxy hop, no engine touch, and nothing here is wired into the build or the server. The UI,
  stats math, log ring and telemetry are vendored from the `serve/` subtree of
  [azido-azide-azide/basalt](https://github.com/azido-azide-azide/basalt) (MIT; the provenance table is in
  [dash/README.md](dash/README.md)); the ninfer operational-log parser, the follower threads, the JSONL reader
  and the route server are new. `dash/tests/` replays committed serve-log and ledger fixtures.

Nothing else is changed: apart from this notice and a `!dash/fixtures/*.log` negation in `.gitignore`, the
diff to upstream touches `dash/` and no other path.

Synced to upstream main at `a0c312e` (v0.13.3, 2026-11-17); on top of it sit the `dash:` commits - this fork's
only code addition - and this notice. The upstream documentation, benchmarks and claims below are upstream's.

**Qwen3.8-Flash-Next (125B MoE), Qwen3.8-27B and Inkling 2 Mini on one or two 16 GB RTX 50-series GPUs, built for agents.**
