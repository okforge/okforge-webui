"""Configuration for the okforge web UI backend.

Everything is a module constant, overridable by environment variable, so
a systemd unit (or any other launcher) can point at different dirs
without code edits. Default paths are layout-relative: everything lives
beside this repo's checkout — the documented Linux install
(/opt/okforge/<repo> → /opt/okforge/{kbs,inbox,quartz,sites}) and a
Windows checkout (C:\\okforge\\<repo> → C:\\okforge\\kbs …) both work
with no environment at all.
"""

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

# This repo's checkout (webui/'s parent) and the base dir beside it.
REPO_DIR = Path(__file__).resolve().parent.parent
_BASE = REPO_DIR.parent

# Load a repo-root .env before reading any OPENKB_WEBUI_* var below, so a
# plain `python -m webui` (no systemd unit) still picks up configuration.
# Done here rather than in __main__.py so every entrypoint benefits — the
# API app and the MCP server both import this module. load_dotenv does NOT
# override variables already set in the environment (systemd/exports win),
# and silently no-ops when the file is absent.
load_dotenv(REPO_DIR / ".env")

# Where the PDF dropdown looks (plus uploads land here).
INBOX_DIR = Path(os.environ.get("OPENKB_WEBUI_INBOX", _BASE / "inbox"))

# Parent directory scanned for KB dirs (anything with a state dir inside —
# see state_dir() below).
KB_ROOT = Path(os.environ.get("OPENKB_WEBUI_KB_ROOT", _BASE / "kbs"))

# Retired KBs are MOVED here (never deleted) — outside KB_ROOT so the
# discovery scan stops seeing them; restore = move the dir back.
RETIRED_DIR = Path(os.environ.get("OPENKB_WEBUI_RETIRED_DIR", _BASE / "kbs-retired"))

# Web-UI "deletes" (inbox PDFs, project markdown, published sites) MOVE
# things here — same archive-first philosophy as RETIRED_DIR. Emptying
# the trash is a deliberate manual act outside the UI.
TRASH_DIR = Path(os.environ.get("OPENKB_WEBUI_TRASH", _BASE / "trash"))

# Per-KB state dir name. STATE_DIR_NAME is what `okforge init` scaffolds as
# of engine v0.8.0; LEGACY_STATE_DIR_NAME is what a not-yet-migrated KB
# still has (`okforge migrate` moves it) — mirrors okforge.config's own
# STATE_DIR_NAME/state_dir(), reimplemented here rather than imported
# since this repo deliberately only shells out to the engine CLI, never
# imports it as a library (see AGENTS.md).
STATE_DIR_NAME = ".okforge"
LEGACY_STATE_DIR_NAME = ".openkb"


def state_dir(kb_dir: Path) -> Path:
    """A KB's state directory: .okforge/ if present, else legacy .openkb/."""
    new = kb_dir / STATE_DIR_NAME
    if new.is_dir():
        return new
    legacy = kb_dir / LEGACY_STATE_DIR_NAME
    if legacy.is_dir():
        return legacy
    return new

# The dir holding .venv with the engine + the okforge-vision-ocr
# package's console scripts (see requirements.txt) — normally this repo.
OPENKB_DIR = Path(os.environ.get("OPENKB_WEBUI_OPENKB_DIR", REPO_DIR))
# Console scripts live in .venv/bin (POSIX) or .venv\Scripts\*.exe (Windows).
_VENV_BIN = OPENKB_DIR / ".venv" / ("Scripts" if os.name == "nt" else "bin")
_EXE = ".exe" if os.name == "nt" else ""
OPENKB_BIN = _VENV_BIN / f"openkb{_EXE}"
OCR_BIN = _VENV_BIN / f"okforge-vision-ocr{_EXE}"
TRANSLATE_BIN = _VENV_BIN / f"okforge-translate-pages{_EXE}"

# LLM endpoints offered in the UI dropdown, as comma-separated
# "label=url[|key[|model]]" entries. Local llama.cpp/vLLM-style servers
# need only "label=url". Hosted services (OpenRouter etc.) append the
# API key and usually a model override in LiteLLM provider/model format,
# e.g.  openrouter=https://openrouter.ai/api/v1|sk-or-v1-...|openrouter/qwen/qwen3.6-27b
# Deployments set this in the systemd unit (deploy.sh passes
# OPENKB_WEBUI_ENDPOINTS through); the default suits a single local server.
ENDPOINTS: dict[str, str] = {}
ENDPOINT_KEYS: dict[str, str] = {}    # only endpoints that need a real key
ENDPOINT_MODELS: dict[str, str] = {}  # only endpoints overriding OPENKB_MODEL
for _part in os.environ.get(
    "OPENKB_WEBUI_ENDPOINTS", "local=http://localhost:8080/v1"
).split(","):
    _label, _, _rest = _part.strip().partition("=")
    _url, _, _extra = _rest.partition("|")
    if not (_label and _url):
        continue
    ENDPOINTS[_label] = _url
    _key, _, _model = _extra.partition("|")
    if _key:
        ENDPOINT_KEYS[_label] = _key
    if _model:
        ENDPOINT_MODELS[_label] = _model
if not ENDPOINTS:
    raise RuntimeError("OPENKB_WEBUI_ENDPOINTS parsed to no endpoints")
DEFAULT_ENDPOINT = os.environ.get(
    "OPENKB_WEBUI_DEFAULT_ENDPOINT", next(iter(ENDPOINTS))
)
if DEFAULT_ENDPOINT not in ENDPOINTS:
    raise RuntimeError(
        f"OPENKB_WEBUI_DEFAULT_ENDPOINT {DEFAULT_ENDPOINT!r} not in ENDPOINTS"
    )

# Model string openkb init expects (LiteLLM format, per README).
OPENKB_MODEL = os.environ.get("OPENKB_WEBUI_MODEL", "openai/Qwen3.6-27B-MTP")


def endpoint_key(label: str) -> str:
    """API key for an endpoint — "no-key" for local servers without one."""
    return ENDPOINT_KEYS.get(label, "no-key")


def endpoint_model(label: str) -> str:
    """LiteLLM model string KBs on this endpoint are initialized with."""
    return ENDPOINT_MODELS.get(label, OPENKB_MODEL)

# Job queue state lives next to this file.
WEBUI_DIR = Path(__file__).resolve().parent
JOBS_DB = WEBUI_DIR / "jobs.sqlite"
JOB_LOG_DIR = WEBUI_DIR / "logs"

# Default chunk size for full-book ingests (pages per OCR→add chunk).
DEFAULT_CHUNK_PAGES = 20

# Standalone OCR outputs (md-out mode): runs not tied to any KB land in
# MD_OUT_DIR/<run-name>/ with the same .md + .pages.json + _images contract
# as a KB's raw/ dir.
MD_OUT_DIR = Path(os.environ.get("OPENKB_WEBUI_MD_OUT", _BASE / "md-out"))

# Quartz publishing (ROADMAP P6): one shared install builds per-KB static
# sites into SITES_DIR/<Subject>/. Making a site public stays a manual
# rsync of that dir to the internet host (KB-OPERATIONS.md).
QUARTZ_DIR = Path(os.environ.get("OPENKB_WEBUI_QUARTZ_DIR", _BASE / "quartz"))
SITES_DIR = Path(os.environ.get("OPENKB_WEBUI_SITES_DIR", _BASE / "sites"))
# Public host a published site's baseUrl points at (og-image/social URLs).
PUBLIC_SITE_HOST = os.environ.get("OPENKB_WEBUI_PUBLIC_SITE_HOST", "localhost")
# node binary (quartz is invoked as `node quartz/bootstrap-cli.mjs` because
# npx often lives outside a service's PATH). PATH lookup first, then the
# usual Linux location.
NODE_BIN = os.environ.get(
    "OPENKB_WEBUI_NODE", shutil.which("node") or "/usr/bin/node"
)
# Where published sites live on the public host — used to render the
# copy-paste go-public rsync command (publishing stays manual by design).
PUBLIC_SITE_DEST = os.environ.get(
    "OPENKB_WEBUI_PUBLIC_SITE_DEST", "user@host:/var/www/sites"
)
