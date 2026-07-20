# AGENTS.md — okforge-webui repo map

This repo is the **manager** for okforge knowledge bases: a FastAPI
backend + vanilla-JS frontend, a strictly serial job queue, an MCP
server, and Apache/systemd deploy machinery. The heavy lifting lives in
two PyPI packages pinned in `requirements.txt`: the
[okforge engine](https://github.com/okforge/okforge) (ingestion, wiki
compilation, query) and
[okforge-vision-ocr](https://github.com/okforge/okforge-vision-ocr)
(page-by-page VLM OCR + translation console scripts).

## Map

- `README.md` — what it does, install, run, configuration (env vars).
- `docs/OPERATIONS.md` — operating KBs day to day: anatomy, copying,
  re-ingest semantics, Obsidian safety, Quartz publishing,
  troubleshooting.
- `webui/PLAN.md` — architecture and API design notes (historical but
  accurate about the shape: five pipeline stages, serial queue).
- `webui/api.py` — FastAPI routes + SSE log tailing.
- `webui/jobs.py` — the job queue and every job runner (`pilot`, `ocr`,
  `translate`, `add`, `full`, `reocr`, `extract`, `recompile`,
  `publish`). The single worker is a load-bearing design decision.
- `webui/kb.py`, `webui/probe.py` — KB discovery/init and PDF probing.
- `webui/config.py` — all configuration; env-var driven, no secrets in
  code (hosted endpoints' API keys arrive via `OKFORGE_WEBUI_ENDPOINTS`,
  `label=url|key|model`).
- `webui/mcp_server.py` — the `/mcp` streamable-HTTP server.
- `webui/static/` — the frontend (no build step).
- `webui/deploy.sh` + `webui/deploy/` — Apache vhost + systemd unit.

## Conventions

- The job queue is **strictly serial by design** — don't parallelize
  `add`s; the engine holds a per-KB ingest lock and single-slot LLM
  hosts choke on concurrent OCR.
- Never restart the backend (or run `deploy.sh`) while a job is running.
- Frontend changes need no restart — rsync `webui/static/` to the
  docroot.
- KB directories stay pure: no tool installs inside them.
- Integration tests use throwaway scratch KBs only; queries are
  read-only and safe anywhere.
