# okforge-webui

A LAN web UI and job runner for [okforge](https://github.com/okforge/okforge)
knowledge bases: drop a scanned book in an inbox and drive it through
VLM OCR, optional translation, and ingestion into an LLM-synthesized
wiki — then browse, query, and publish the result. Built for local-first
setups where the LLM is your own llama.cpp/vLLM box, not a cloud API.

The pipeline is five screens: **probe** (inspect the PDF: text layer?
language? page count) → **pilot** (OCR a page or three, check the
transcription and image crops before committing) → **KB setup** →
**run** (chunked OCR → translate → add jobs with live progress) →
**verify** (wiki browser, query console, site publishing).

## What's in the box

- **Serial job queue** (sqlite + one worker, on purpose): one `add` or
  OCR run at a time protects single-slot LLM hosts and the engine's
  per-KB ingest lock. Jobs survive backend restarts; every finished job
  keeps its log. One-click **resume/retry**, a **stall watchdog**
  (flags, never kills), per-chunk **ETA** from real history, and a git
  **pre-ingest snapshot** of the KB before every add.
- **OCR + image extraction** via
  [okforge-vision-ocr](https://github.com/okforge/okforge-vision-ocr)
  (one VLM call per page: markdown transcription + photo bounding boxes
  together), with a table mode for pages the fast path mangles and a
  per-page re-OCR + re-ingest repair loop.
- **Translation** workflow for non-English scans: faithful transcription
  first, page-by-page translation second — both language versions share
  one image directory and page citations survive.
- **Wiki browser** with lexical search (source hits carry real page
  numbers), image lightbox, and markdown rendering.
- **MCP server** at `/mcp` (streamable HTTP): `list_projects`,
  `project_status`, `ask`, `search`, `read_wiki_page`. Connect any MCP
  client, e.g. `claude mcp add --transport http okforge http://<host>/mcp`.
- **Static-site publishing** per KB via [Quartz](https://quartz.jzhao.xyz/)
  — full-text search, graph view, backlinks — one button, then a printed
  rsync command to go public.

## Directory layout

```
/opt/okforge/
    tooling/            ← this repo; .venv/ inside it
    kbs/<Subject>/      ← one self-contained knowledge base per subject
    inbox/              ← PDF drop point for the web UI
    quartz/             ← shared Quartz install (site publishing, optional)
    sites/<Subject>/    ← published static sites (optional)
```

Every KB is self-contained (sources, wiki, engine state, `.env`, its own
git history) — copy the directory and you've copied the KB. The UI
discovers KBs by scanning the KB root; nothing is registered anywhere
else.

## Install

```bash
git clone https://github.com/okforge/okforge-webui /opt/okforge/tooling
cd /opt/okforge/tooling
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p /opt/okforge/kbs /opt/okforge/inbox
```

`requirements.txt` pins the two okforge packages from PyPI — the
[okforge engine](https://github.com/okforge/okforge) (ingestion, wiki
compilation, query; see its
[GETTING_STARTED](https://github.com/okforge/okforge/blob/main/GETTING_STARTED.md))
and [okforge-vision-ocr](https://github.com/okforge/okforge-vision-ocr)
(pre-conversion console scripts) — plus the FastAPI backend's own
dependencies.

## Run it

Development (one process serves the static frontend and the API):

```bash
.venv/bin/uvicorn webui.api:app --host 127.0.0.1 --port 8500
# browse http://localhost:8500/
```

Production is Apache (static docroot + `/api/` reverse proxy) in front of
the same backend under systemd:

```bash
SERVER_NAME=okforge.local OPENKB_WEBUI_ENDPOINTS="gpu1=http://gpu1:8080/v1" \
    webui/deploy.sh
```

`deploy.sh` is idempotent — rerun it after changes (it restarts the
backend, so **never while a job is running**). Frontend-only changes are
just `sudo rsync -a --delete webui/static/ /var/www/okforge-webui/`.

## Configuration

Everything is an environment variable (set them in the systemd unit —
`deploy.sh` passes any that are exported when it runs):

| variable | default | meaning |
|---|---|---|
| `OPENKB_WEBUI_ENDPOINTS` | `local=http://localhost:8080/v1` | LLM endpoints for the UI dropdown, `label=url,label=url` |
| `OPENKB_WEBUI_DEFAULT_ENDPOINT` | first label | pre-selected endpoint |
| `OPENKB_WEBUI_MODEL` | `openai/Qwen3.6-27B-MTP` | model string new KBs are initialized with |
| `OPENKB_WEBUI_KB_ROOT` | `/opt/okforge/kbs` | where KBs live |
| `OPENKB_WEBUI_INBOX` | `/opt/okforge/inbox` | PDF drop dir |
| `OPENKB_WEBUI_QUARTZ_DIR` | `/opt/okforge/quartz` | shared Quartz install |
| `OPENKB_WEBUI_SITES_DIR` | `/opt/okforge/sites` | published-site output |
| `OPENKB_WEBUI_PUBLIC_SITE_HOST` | `localhost` | public host for published sites' baseUrl |
| `OPENKB_WEBUI_PUBLIC_SITE_DEST` | `user@host:/var/www/sites` | rsync target shown by the go-public helper |
| `OPENKB_WEBUI_NODE` | `/usr/bin/node` | node binary for Quartz builds under systemd |

Each KB additionally carries its own `.env` (`OPENAI_API_BASE`,
`LLM_API_KEY`) and `config.yaml`. One setting matters more than all the
others on llama.cpp hosts serving Qwen-family models with thinking
enabled by default — without it every ingest silently pays a hidden
reasoning block (measured: a 2-minute add becomes 27):

```yaml
llm_extra_body:
  chat_template_kwargs:
    enable_thinking: false
```

KBs created through the web UI get this automatically.

## Docs

- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — operating KBs day to day:
  anatomy, copying between machines, re-ingest semantics (what happens
  when you add the same document twice), Obsidian editing safety, Quartz
  publishing, troubleshooting.
- [`webui/PLAN.md`](webui/PLAN.md) — architecture and API design notes.

## License

MIT — see [LICENSE](LICENSE).
