# okforge-webui

A LAN web UI and job runner for [okforge](https://github.com/okforge/okforge)
knowledge bases: drop a scanned book in an inbox and drive it through
VLM OCR, optional translation, and ingestion into an LLM-synthesized
wiki — then browse, query, and publish the result. Built for local-first
setups where the LLM is your own llama.cpp/vLLM box, not a cloud API.

The pipeline is five screens: **probe** (inspect the PDF: text layer?
language? page count) → **pilot** (OCR a page or three, check the
transcription and image crops before committing) → **project** (pick or
name the project that collects the output) → **run** (chunked OCR →
translate → markdown with live progress) → **verify & use** (review the
markdown, ingest it into the project's knowledge base, then query and
publish). OCR and ingestion are always separate steps, so the same tool
doubles as a pure PDF→markdown converter — skip the ingest and take the
files from `md-out/`.

## What's in the box

- **Serial job queue** (sqlite + one worker, on purpose): one `add` or
  OCR run at a time protects single-slot LLM hosts and the engine's
  per-KB ingest lock. Jobs survive backend restarts; every finished job
  keeps its log. One-click **resume/retry**, a **stall watchdog**
  (flags, never kills), per-chunk **ETA** from real history, and a git
  **pre-ingest snapshot** of the KB before every add.
- **Markdown first, ingest second**: every run OCRs into the project's
  `md-out/<name>/` folder (chunked `.md` + page maps + image crops);
  ingesting that markdown into the knowledge base is a separate step —
  a one-click button in the verify stage (KB stats update chunk by
  chunk) or an auto-ingest toggle on the run. The KB is created on
  first ingest — or never, if all you wanted was the markdown.
- **Archive-first deletes**: removing an uploaded PDF, a project's
  markdown, a published site, or a whole project **moves** it to
  `trash/` (KBs retire to `kbs-retired/`) — nothing in the UI is
  destructive, restore is a `mv` back.
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
  Clients that don't surface MCP server instructions (Open-WebUI and
  other OpenAPI-bridged clients) should get the recommended system
  prompt from [`docs/MCP_CLIENT_PROMPT.md`](docs/MCP_CLIENT_PROMPT.md).
- **Static-site publishing** per KB via [Quartz](https://quartz.jzhao.xyz/)
  — full-text search, graph view, backlinks — one button, then a printed
  rsync command to go public.

## Prerequisites

- **Python 3.10+**
- **Git** — for the clone; on Windows it also provides the `grep`
  binary the engine's query agent uses.
- **Node.js 18+** — only for the optional static-site publishing
  (Quartz); everything else runs without it.

## Directory layout

```
<base>/                 e.g. /opt/okforge/  or  C:\okforge\
    okforge-webui/      ← this repo; .venv/ inside it
    kbs/<Subject>/      ← one self-contained knowledge base per subject
    inbox/              ← PDF drop point for the web UI
    md-out/<Project>/   ← OCR'd markdown per project (created on demand)
    kbs-retired/        ← retired KBs (archive-first "delete")
    trash/              ← web-UI deletes move things here, never erase
    quartz/             ← shared Quartz install (site publishing, optional)
    sites/<Subject>/    ← published static sites (optional)
```

The base directory can be anywhere — all defaults are relative to where
this repo sits (each is individually overridable by env var, see
Configuration). Every KB is self-contained (sources, wiki, engine state,
`.env`, its own git history) — copy the directory and you've copied the
KB. The UI discovers KBs by scanning the KB root; nothing is registered
anywhere else.

## Install

Linux/macOS:

```bash
cd /opt/okforge   # or any base dir
git clone https://github.com/okforge/okforge-webui
cd okforge-webui
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p ../kbs ../inbox
```

Windows (PowerShell):

```powershell
cd C:\okforge     # or any base dir
git clone https://github.com/okforge/okforge-webui
cd okforge-webui
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
md ..\kbs, ..\inbox
```

(PowerShell gotcha: anything in the *current* directory needs a `.\`
prefix to run — `.\script.ps1`, not `script.ps1`. The `.venv\Scripts\…`
forms above already qualify.)

`requirements.txt` pins the two okforge packages from PyPI — the
[okforge engine](https://github.com/okforge/okforge) (ingestion, wiki
compilation, query; see its
[GETTING_STARTED](https://github.com/okforge/okforge/blob/main/GETTING_STARTED.md))
and [okforge-vision-ocr](https://github.com/okforge/okforge-vision-ocr)
(pre-conversion console scripts) — plus the FastAPI backend's own
dependencies.

## Run it

One process serves the frontend, the API, and the MCP server. Run it
**from this repo's directory** — `python -m webui` resolves the `webui`
package relative to the current dir, so from anywhere else Python exits
with `No module named webui`:

```bash
cd <base>/okforge-webui
.venv/bin/python -m webui          # Linux/macOS
.venv\Scripts\python -m webui      # Windows
# browse http://<host>:8500/
```

`OPENKB_WEBUI_HOST` / `OPENKB_WEBUI_PORT` change the bind (default
`0.0.0.0:8500` — LAN-visible; use `127.0.0.1` to keep it local). Same
trust model in every mode: LAN-only, no auth — don't expose it beyond a
network you trust.

To run it as a service: on Linux,
[`webui/deploy/okforge-webui-standalone.service`](webui/deploy/okforge-webui-standalone.service)
is a ready-to-edit systemd unit; on Windows, use Task Scheduler
("At startup", run `<repo>\.venv\Scripts\python.exe -m webui`) or
[NSSM](https://nssm.cc/).

### Optional: Apache in front (Linux)

For port 80, a LAN vhost name, and an easy basic-auth option, `deploy.sh`
installs Apache (static docroot + `/api/` reverse proxy) in front of the
same backend under systemd:

```bash
SERVER_NAME=okforge.local OPENKB_WEBUI_ENDPOINTS="gpu1=http://gpu1:8080/v1" \
    webui/deploy.sh
```

`deploy.sh` is idempotent — rerun it after changes (it restarts the
backend, so **never while a job is running**). In this mode frontend
files are served by Apache, so frontend-only changes are
`sudo rsync -a --delete webui/static/ /var/www/okforge-webui/`
(standalone mode serves them straight from the repo — nothing to copy).

## Configuration

Everything is an environment variable (for the systemd deployments, set
them in the unit — `deploy.sh` passes any that are exported when it
runs). `<base>` below means the directory this repo sits in:

| variable | default | meaning |
|---|---|---|
| `OPENKB_WEBUI_HOST` | `0.0.0.0` | bind address (`python -m webui`) |
| `OPENKB_WEBUI_PORT` | `8500` | bind port (`python -m webui`) |
| `OPENKB_WEBUI_ENDPOINTS` | `local=http://localhost:8080/v1` | LLM endpoints for the UI dropdown, comma-separated `label=url[\|key[\|model]]` — key and model only for hosted services (see below) |
| `OPENKB_WEBUI_DEFAULT_ENDPOINT` | first label | pre-selected endpoint |
| `OPENKB_WEBUI_MODEL` | `openai/Qwen3.6-27B-MTP` | model string new KBs are initialized with (per-endpoint `model` overrides it) |
| `OPENKB_WEBUI_KB_ROOT` | `<base>/kbs` | where KBs live |
| `OPENKB_WEBUI_INBOX` | `<base>/inbox` | PDF drop dir |
| `OPENKB_WEBUI_MD_OUT` | `<base>/md-out` | per-project OCR'd markdown |
| `OPENKB_WEBUI_RETIRED_DIR` | `<base>/kbs-retired` | where retired KBs move |
| `OPENKB_WEBUI_TRASH` | `<base>/trash` | where web-UI deletes move things |
| `OPENKB_WEBUI_QUARTZ_DIR` | `<base>/quartz` | shared Quartz install |
| `OPENKB_WEBUI_SITES_DIR` | `<base>/sites` | published-site output |
| `OPENKB_WEBUI_PUBLIC_SITE_HOST` | `localhost` | public host for published sites' baseUrl |
| `OPENKB_WEBUI_PUBLIC_SITE_DEST` | `user@host:/var/www/sites` | rsync target shown by the go-public helper |
| `OPENKB_WEBUI_NODE` | `node` on PATH, else `/usr/bin/node` | node binary for Quartz builds |

Local llama.cpp/vLLM endpoints need only `label=url`. A hosted
OpenAI-compatible service takes two more `|`-separated fields — its API
key and the model in LiteLLM `provider/model` format:

```
OPENKB_WEBUI_ENDPOINTS="gpu1=http://gpu1:8080/v1,openrouter=https://openrouter.ai/api/v1|sk-or-v1-...|openrouter/qwen/qwen3.6-27b"
```

KBs created on such an endpoint get the key in their `.env`, the model
in their `config.yaml`, and a provider-appropriate thinking-off block
(OpenRouter's `reasoning.enabled=false` instead of llama.cpp's
`chat_template_kwargs`). OCR/translate jobs strip the LiteLLM provider
prefix and pass the rest as the OpenAI-protocol model name. Note the
key lives in plain text in the unit file/environment — acceptable on a
single-operator LAN box; protect the systemd drop-in accordingly
(`chmod 640`).

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

One more expectation worth setting for large collections: ingest cost
scales with the size of the *wiki*, not the document being added —
recurring concept and entity pages are rewritten on every mention — so
a document that takes 3 minutes into a fresh KB takes ~9 into the same
KB 300 documents later. llama.cpp's prefix cache absorbs most of the
growth (84% of input tokens were cache hits over a measured
364-document run); see
[`docs/OPERATIONS.md`](docs/OPERATIONS.md#ingest-cost-at-collection-scale-measured)
for the measured figures before planning a multi-day run.

## Docs

- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — operating KBs day to day:
  anatomy, copying between machines, re-ingest semantics (what happens
  when you add the same document twice), Obsidian editing safety, Quartz
  publishing, troubleshooting.
- [`webui/PLAN.md`](webui/PLAN.md) — architecture and API design notes.

## License

MIT — see [LICENSE](LICENSE).
