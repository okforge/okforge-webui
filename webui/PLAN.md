# okforge Web UI — Plan

> Historical design doc — some names below predate the okforge rename
> (2026-07-06: service `okforge-webui`, default vhost `okforge.local`).
> Current procedures: the repo README and `docs/OPERATIONS.md`.

A browser frontend for the scan → OCR → (translate) → ingest → query
pipeline, hosted on Apache. Written 2026-07-04, after the
SpanishGunsAndPistols manual walkthrough proved the step sequence; the UI
is that walkthrough turned into screens.

## Architecture

Apache cannot run the pipeline (long-lived Python jobs, hours of LLM
calls), so it plays to its strength — static files + reverse proxy:

```
Browser ── Apache :80 ──┬── /            static HTML/CSS/JS  (webui/static/)
                        └── /api/...     ProxyPass → 127.0.0.1:8500
                                          FastAPI app (openkb-webui.service)
                                          │  wraps: okforge-vision-ocr,
                                          │         okforge-translate-pages,
                                          │         openkb CLI
                                          └─ single-worker job queue
```

- **Frontend**: vanilla HTML/JS/CSS, no build step — copy `static/` into
  the vhost and it works. One page, five stages (below), state fetched
  from the API.
- **Backend**: FastAPI + uvicorn on `127.0.0.1:8500`, run from the
  repo's `.venv`, managed by a systemd unit. All filesystem
  and subprocess work happens here, as the operator's user, so KBs stay
  exactly where the CLI workflow uses them.
- **Job queue**: one worker thread, strictly serial. This is a feature,
  not a limitation — it structurally enforces the two rules we keep
  having to remember by hand: never two `openkb add`s at once, and never
  concurrent OCR runs against the (2-slot) llama.cpp server. Job state in
  `webui/jobs.sqlite`; per-job log files; jobs survive a backend restart
  (resume = re-queue unfinished chunks; OCR re-runs are cheap and
  idempotent, adds are checked against `openkb list` first).

## The five stages (mirroring the manual walkthrough)

1. **Probe** — pick a PDF (dropdown of a configured inbox dir, e.g.
   `~/Desktop`, plus upload). Backend returns page count, text-layer page
   count, first extractable text, embedded-image resolution. UI verdict:
   "text layer → plain `openkb add`" vs "pure scan → OCR pipeline", and a
   language guess with a "translate to English?" toggle.
2. **Pilot** — the step that caught `--figures`: OCR 1–3 chosen pages,
   show transcription beside the rendered page image and any crops, with
   a photos-only / `--figures` toggle and a "re-run this page" button.
   Nothing proceeds until the pilot looks right.
3. **Set up KB** — name the KB dir, backend runs `openkb init -l en`
   non-interactively and writes `.env` (endpoint dropdown from the
   configured `OPENKB_WEBUI_ENDPOINTS`). Existing KBs listed for adding
   more docs.
4. **Run** — chunk plan (default 20 pages, editable), then queued jobs
   per chunk: OCR → (translate) → add. Live progress via SSE tail of the
   job log, parsed into a per-page progress bar and a crop thumbnail
   strip as images appear. Header shows an LLM-server busy light (polls
   `/slots?model=...`) so ballooning add times read as "queued", not hung.
5. **Verify & use** — stats (docs / concepts / entities / images /
   citation counts), a query box (`openkb query`, streamed), and a wiki
   browser (summaries, concept/entity pages, image gallery served
   through the API).

## API sketch

```
GET  /api/inbox                       list PDFs in the inbox dir
POST /api/probe                       {pdf} → pages, text stats, sample
GET  /api/kbs                         list KB dirs + status
POST /api/kbs                         {name, lang, endpoint} → init + .env
POST /api/jobs                        {type: pilot|ocr|translate|add|full,
                                       kb, pdf, pages, figures, translate}
GET  /api/jobs                        queue + history
GET  /api/jobs/{id}/events            SSE: log lines, page progress, crops
POST /api/kb/{name}/query             {question} → streamed answer
GET  /api/kb/{name}/wiki/{path}       wiki markdown / images (read-only)
GET  /api/server/slots                LLM busy indicator
```

`full` is the composite job: expands to per-chunk OCR→(translate)→add,
so a whole book is one click after the pilot is approved.

## Apache deployment

```apache
<VirtualHost *:80>
    ServerName openkb.local
    DocumentRoot /var/www/openkb-webui          # rsync of webui/static/
    ProxyPass        /api/ http://127.0.0.1:8500/api/
    ProxyPassReverse /api/ http://127.0.0.1:8500/api/
    # SSE needs: ProxyPass ... flushpackets=on ; no output buffering
    LimitRequestBody 524288000                  # 500 MB PDF uploads
</VirtualHost>
```

Modules: `proxy`, `proxy_http`. Backend unit: `okforge-webui.service`
(ExecStart=`<repo>/.venv/bin/uvicorn webui.api:app --port 8500`, run as
the operator's user). LAN-only, no auth in v1 by deliberate choice; add
Apache basic-auth if it ever faces a wider network.

## Decisions & risks

- **Vanilla JS over React/Vue**: no node toolchain on this machine, no
  build step to document, and the UI is forms + log tails + galleries.
- **SSE over WebSockets**: one-directional progress is all we need and it
  proxies through Apache with plain `mod_proxy_http`.
- **Serial queue**: throughput equals today's CLI throughput — fine,
  the LLM server is the bottleneck either way.
- **Uploads vs inbox**: both; big scans are already on this machine, so
  the inbox dropdown is the common path and uploads are the convenience.
- **Risk — interactive `openkb init`**: backend must use the
  `printf '\n' |` trick (or pass a pty); covered by an integration test.
- **Risk — job crash mid-book**: chunk-level granularity + idempotent
  OCR + add-only-if-not-listed makes "Resume" safe.
- **Out of scope v1**: multi-user, auth, editing wiki pages, deleting
  KBs from the UI (destructive — CLI only), remote (non-localhost) LLM
  server management.

## Build order

1. Backend skeleton: FastAPI, config (inbox dir, endpoints), `/api/probe`,
   `/api/kbs` list/init. Testable with curl.
2. Job queue + OCR job type + SSE log streaming. CLI parity reached.
3. Static frontend: stages 1–4 against the real API.
4. Pilot/review screen with crop gallery + figures toggle + re-run page.
5. Translate + add job types, `full` composite, resume logic.
6. Verify stage: stats, query box, wiki browser.
7. Apache vhost + systemd unit + deploy script (`webui/deploy.sh`),
   docs in this file's repo (README section + AGENTS.md pointer).

Each step ends runnable; steps 1–2 give a usable curl-driven API even
before any HTML exists.
