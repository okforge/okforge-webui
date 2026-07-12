"""FastAPI backend for the okforge web UI (PLAN.md).

Run from the repo checkout with:
    .venv/bin/uvicorn webui.api:app --port 8500

All endpoints live under /api/ so Apache can ProxyPass that prefix and
serve webui/static/ itself; for development uvicorn serves both.
"""

import asyncio
import contextlib
import json
import re
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from . import config, jobs, kb, mcp_server, probe


@contextlib.asynccontextmanager
async def _lifespan(app):
    jobs.start_worker()
    # mounted sub-apps don't get their lifespan run, so drive the MCP
    # session manager from ours
    async with mcp_server.mcp.session_manager.run():
        yield


app = FastAPI(title="okforge Web UI", version="0.1", lifespan=_lifespan)


# ---------------------------------------------------------------- inbox

@app.get("/api/inbox")
def get_inbox():
    pdfs = []
    if config.INBOX_DIR.is_dir():
        for p in sorted(config.INBOX_DIR.glob("*.pdf"), key=lambda p: p.name.lower()):
            st = p.stat()
            pdfs.append({
                "name": p.name,
                "path": str(p),
                "size": st.st_size,
                "mtime": int(st.st_mtime),
            })
    return {"inbox_dir": str(config.INBOX_DIR), "pdfs": pdfs}


@app.post("/api/inbox")
async def upload_pdf(file: UploadFile):
    name = Path(file.filename or "").name
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "only .pdf uploads are accepted")
    dest = config.INBOX_DIR / name
    if dest.exists():
        raise HTTPException(409, f"{name} already exists in the inbox")
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1 << 20):
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    st = dest.stat()
    return {"name": name, "path": str(dest), "size": st.st_size}


# ---------------------------------------------------------------- probe

class ProbeRequest(BaseModel):
    pdf: str


@app.post("/api/probe")
def post_probe(req: ProbeRequest):
    try:
        return probe.probe_pdf(req.pdf)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/page-image")
def get_page_image(pdf: str, page: int, width: int = 900):
    """Rendered page PNG for the pilot review (transcription beside page)."""
    try:
        data = probe.render_page_png(pdf, page, min(width, 2000))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Response(data, media_type="image/png")


# ------------------------------------------------------------------ kbs

class KbCreate(BaseModel):
    name: str
    lang: str = "en"
    endpoint: str = config.DEFAULT_ENDPOINT


@app.get("/api/kbs")
def get_kbs():
    return {"kbs": kb.list_kbs(), "endpoints": list(config.ENDPOINTS)}


@app.post("/api/kbs", status_code=201)
def post_kbs(req: KbCreate):
    try:
        return kb.init_kb(req.name, req.lang, req.endpoint)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.delete("/api/kbs/{name}")
def delete_kb(name: str):
    """Retire a KB: archive-first (ROADMAP P11) — the dir MOVES to
    RETIRED_DIR, nothing is deleted; restore = move it back."""
    try:
        kb.resolve_kb(name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    active = jobs.active_count_for_kb(name)
    if active:
        raise HTTPException(
            409,
            f"KB {name!r} has {active} queued/running job(s) — "
            "wait for them or cancel them first",
        )
    try:
        return kb.retire_kb(name)
    except OSError as e:
        raise HTTPException(500, f"retire failed: {e}")


# ----------------------------------------------------------------- jobs

class JobCreate(BaseModel):
    type: str
    kb: str | None = None
    pdf: str | None = None
    path: str | None = None  # add jobs: the file to `openkb add`
    pages: str | int | None = None
    figures: bool = False
    tables: bool = False  # pilot/reocr: thinking + table-reconstruction prompt
    translate: bool = False
    src_lang: str | None = None  # probe's language guess ("ca", "es", …)
    text_layer: bool = False  # full jobs: pymupdf extraction instead of OCR
    endpoint: str | None = None
    chunk_pages: int = config.DEFAULT_CHUNK_PAGES
    prompt_extra: str | None = None  # free text appended to the OCR prompt


def _validate_job(req: JobCreate) -> dict:
    """Fail fast at enqueue time, not when the worker gets there."""
    if req.type not in jobs.JOB_TYPES:
        raise ValueError(f"unknown job type: {req.type!r}")
    if req.src_lang is not None and not re.match(r"^[a-z]{2}$", req.src_lang):
        raise ValueError(f"bad src_lang: {req.src_lang!r}")
    prompt_extra = (req.prompt_extra or "").strip() or None
    if prompt_extra and len(prompt_extra) > 500:
        raise ValueError("prompt_extra too long (max 500 chars)")
    params = {
        "pdf": None,
        "path": None,
        "pages": str(req.pages) if req.pages is not None else None,
        "figures": req.figures,
        "tables": req.tables,
        "translate": req.translate,
        "src_lang": req.src_lang,
        "text_layer": req.text_layer,
        "endpoint": req.endpoint,
        "chunk_pages": req.chunk_pages,
        "prompt_extra": prompt_extra,
    }
    if req.type in {"pilot", "ocr", "translate", "full", "reocr", "extract", "recompile"}:
        if not req.pdf:
            raise ValueError(f"{req.type} job requires a pdf")
        params["pdf"] = str(probe.allowed_pdf_path(req.pdf))
    if req.type == "pilot":
        if params["pages"] is None:
            raise ValueError("pilot job requires pages (e.g. \"16\" or \"5-7\")")
        if req.endpoint and req.endpoint not in config.ENDPOINTS:
            raise ValueError(f"unknown endpoint: {req.endpoint!r}")
        return params
    if req.type in ("reocr", "recompile") and (
        params["pages"] is None or "-" in params["pages"]
    ):
        raise ValueError(f'{req.type} takes exactly one page, e.g. pages="42"')
    if not req.kb:
        raise ValueError(f"{req.type} job requires a kb")
    kb_dir = kb.resolve_kb(req.kb)  # raises on unknown/invalid
    if req.type == "add":
        if not req.path:
            raise ValueError("add job requires a path")
        p = Path(req.path).expanduser().resolve()
        if p.is_relative_to((kb_dir / "raw").resolve()):
            # chunk outputs may not exist yet at enqueue time; the runner
            # checks existence when its turn comes
            params["path"] = str(p)
        else:
            params["path"] = str(probe.allowed_pdf_path(req.path))
    return params


@app.post("/api/jobs", status_code=201)
def post_jobs(req: JobCreate):
    try:
        params = _validate_job(req)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return jobs.enqueue(req.type, params, kb_name=req.kb)


@app.get("/api/jobs")
def get_jobs(limit: int = 100):
    return {"jobs": jobs.list_jobs(limit=limit)}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"no such job: {job_id}")
    job["children"] = jobs.children(job_id)
    return job


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    try:
        return jobs.cancel(job_id)
    except KeyError:
        raise HTTPException(404, f"no such job: {job_id}")


@app.post("/api/jobs/{job_id}/retry", status_code=201)
def retry_job(job_id: int):
    """Re-enqueue a finished job with identical params. On a `full` job
    this is Resume: it re-expands with the same range + chunk size and
    skips work already done."""
    try:
        return jobs.retry(job_id)
    except KeyError:
        raise HTTPException(404, f"no such job: {job_id}")
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/jobs/{job_id}/log", response_class=PlainTextResponse)
def get_job_log(job_id: int):
    if jobs.get_job(job_id) is None:
        raise HTTPException(404, f"no such job: {job_id}")
    lp = jobs.log_path(job_id)
    return lp.read_text(encoding="utf-8") if lp.exists() else ""


@app.get("/api/jobs/{job_id}/files/{rel_path:path}")
def get_job_file(job_id: int, rel_path: str):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"no such job: {job_id}")
    base = jobs.job_files_dir(job)
    target = (base / rel_path).resolve()
    if not target.is_relative_to(base.resolve()) or not target.is_file():
        raise HTTPException(404, f"no such file: {rel_path}")
    return FileResponse(target)


# Progress lines both okforge-vision-ocr console scripts print on stderr:
# "Page N: image WxH, calling model..." or "Page N: translating...", and
# crop saves "  page N photo P: WxH -> rel".
_PAGE_RE = re.compile(r"^Page (\d+): ")
_IMAGE_RE = re.compile(r"^\s+page (\d+) photo \d+: \d+x\d+ -> (\S+\.jpe?g)")


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: int, request: Request):
    if jobs.get_job(job_id) is None:
        raise HTTPException(404, f"no such job: {job_id}")

    async def gen():
        lp = jobs.log_path(job_id)
        pos = 0
        pending = ""
        last_status = None
        while True:
            if await request.is_disconnected():
                return
            job = jobs.get_job(job_id)

            if lp.exists():
                with lp.open("r", encoding="utf-8") as f:
                    f.seek(pos)
                    pending += f.read()
                    pos = f.tell()
            # Emit only complete lines; keep a partial tail for next poll.
            *lines, pending = pending.split("\n")
            for line in lines:
                yield {"event": "log", "data": line}
                m = _PAGE_RE.match(line)
                if m:
                    yield {"event": "progress",
                           "data": json.dumps({"page": int(m.group(1))})}
                m = _IMAGE_RE.match(line)
                if m:
                    yield {"event": "image", "data": json.dumps({
                        "page": int(m.group(1)),
                        "url": f"/api/jobs/{job_id}/files/{m.group(2)}",
                    })}

            if job["status"] != last_status:
                last_status = job["status"]
                yield {"event": "status", "data": json.dumps(
                    {"status": job["status"], "error": job["error"]})}
            if job["status"] in jobs.TERMINAL:
                return
            await asyncio.sleep(0.7)

    return EventSourceResponse(gen())


# ------------------------------------------------- verify stage: query/wiki

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class QueryRequest(BaseModel):
    question: str


@app.post("/api/kb/{name}/query")
async def kb_query(name: str, req: QueryRequest):
    """Stream `openkb query --raw` output as plain text. Runs outside the
    job queue: queries are interactive and read-only."""
    try:
        kb_dir = kb.resolve_kb(name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "empty question")

    import os as _os
    env = _os.environ.copy()
    env.update({"NO_COLOR": "1", "TERM": "dumb"})
    proc = await asyncio.create_subprocess_exec(
        str(config.OPENKB_BIN), "--kb-dir", str(kb_dir), "query", "--raw",
        question,
        cwd=kb_dir,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def gen():
        try:
            while True:
                chunk = await proc.stdout.read(1024)
                if not chunk:
                    break
                yield _ANSI_RE.sub("", chunk.decode("utf-8", errors="replace"))
            rc = await proc.wait()
            if rc != 0:
                yield f"\n[openkb query exited {rc}]\n"
        finally:
            if proc.returncode is None:
                proc.terminate()

    from fastapi.responses import StreamingResponse
    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


_WIKI_TEXT_EXTS = {".md", ".txt", ".yaml", ".json"}
_WIKI_IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".png": "image/png", ".gif": "image/gif",
                     ".webp": "image/webp"}


@app.get("/api/kb/{name}/wiki")
@app.get("/api/kb/{name}/wiki/{rel_path:path}")
def get_wiki(name: str, rel_path: str = ""):
    """Read-only wiki browser: directories -> JSON listing, .md -> text,
    images -> the file. Editing stays CLI-only (PLAN.md out-of-scope)."""
    try:
        kb_dir = kb.resolve_kb(name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    base = (kb_dir / "wiki").resolve()
    if not base.is_dir():
        raise HTTPException(404, f"{name} has no wiki/ yet")
    target = (base / rel_path).resolve() if rel_path else base
    if not target.is_relative_to(base) or not target.exists():
        raise HTTPException(404, f"no such wiki path: {rel_path}")
    if target.is_dir():
        dirs, files = [], []
        for p in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            rel = p.relative_to(base).as_posix()
            if p.is_dir():
                dirs.append(rel)
            elif p.suffix.lower() in _WIKI_TEXT_EXTS or \
                    p.suffix.lower() in _WIKI_IMAGE_TYPES:
                files.append(rel)
        return {"kb": name, "path": rel_path, "dirs": dirs, "files": files}
    suffix = target.suffix.lower()
    if suffix in _WIKI_IMAGE_TYPES:
        return FileResponse(target, media_type=_WIKI_IMAGE_TYPES[suffix])
    if suffix in _WIKI_TEXT_EXTS:
        return PlainTextResponse(target.read_text(encoding="utf-8"))
    raise HTTPException(404, f"unsupported file type: {rel_path}")


# raw/ additionally holds the original PDFs the engine archived at add time.
_RAW_BINARY_TYPES = dict(_WIKI_IMAGE_TYPES, **{".pdf": "application/pdf"})


@app.get("/api/kb/{name}/raw")
@app.get("/api/kb/{name}/raw/{rel_path:path}")
def get_raw(name: str, rel_path: str = ""):
    """Read-only access to raw/ — the pristine ingested sources
    (<stem>_pN_M.md chunks, .pages.json page arrays, _images/ crops,
    archived PDFs) so external pipelines can pull them (ROADMAP P12:
    feed a separate RAG). Directories return a flat RECURSIVE listing
    with size + mtime — one call enumerates everything a pipeline needs
    to sync; files are served directly. Hidden entries (.reocr_job*
    scratch dirs) are skipped."""
    try:
        kb_dir = kb.resolve_kb(name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    base = (kb_dir / "raw").resolve()
    if not base.is_dir():
        raise HTTPException(404, f"{name} has no raw/ yet")
    target = (base / rel_path).resolve() if rel_path else base
    if not target.is_relative_to(base) or not target.exists():
        raise HTTPException(404, f"no such raw path: {rel_path}")
    if target.is_dir():
        files = []
        for p in sorted(target.rglob("*"), key=lambda p: p.as_posix().lower()):
            rel = p.relative_to(base)
            if any(part.startswith(".") for part in rel.parts):
                continue
            suffix = p.suffix.lower()
            if not p.is_file() or (suffix not in _WIKI_TEXT_EXTS
                                   and suffix not in _RAW_BINARY_TYPES):
                continue
            st = p.stat()
            files.append({"path": rel.as_posix(),
                          "size": st.st_size, "mtime": st.st_mtime})
        return {"kb": name, "path": rel_path, "files": files}
    suffix = target.suffix.lower()
    if suffix in _RAW_BINARY_TYPES:
        return FileResponse(target, media_type=_RAW_BINARY_TYPES[suffix])
    if suffix in _WIKI_TEXT_EXTS:
        return PlainTextResponse(target.read_text(encoding="utf-8"))
    raise HTTPException(404, f"unsupported file type: {rel_path}")


@app.get("/api/kb/{name}/search")
def kb_search(name: str, q: str, limit: int = 20):
    """Fast lexical wiki search (no LLM) — same engine as the MCP search
    tool; source hits carry real page numbers."""
    from .mcp_server import search as _search

    try:
        return {"results": _search(name, q, max_results=min(limit, 100))}
    except ValueError as e:
        raise HTTPException(404 if "no such KB" in str(e) else 400, str(e))


@app.get("/api/kb/{name}/site")
@app.get("/api/kb/{name}/site/{rel_path:path}")
def kb_site(name: str, request: Request, rel_path: str = ""):
    """Serve the KB's published Quartz site (built by a `publish` job into
    SITES_DIR/<name>). Quartz emits extensionless pretty URLs, so this does
    the MultiViews dance: try the path, then path.html, then dir/index.html.
    Rides the existing /api/ Apache proxy — no vhost changes needed."""
    try:
        kb.resolve_kb(name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    base = (config.SITES_DIR / name).resolve()
    if not (base / "index.html").is_file():
        raise HTTPException(404, f"{name} has no published site — run Publish first")
    # Bare /site: relative asset links need the trailing slash to resolve.
    if not rel_path and not request.url.path.endswith("/"):
        return Response(status_code=307, headers={"Location": request.url.path + "/"})
    target = (base / rel_path).resolve() if rel_path else base / "index.html"
    if not target.is_relative_to(base):
        raise HTTPException(404, f"no such site path: {rel_path}")
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        html = target.with_name(target.name + ".html")
        if html.is_file():
            target = html
        else:
            not_found = base / "404.html"
            if not_found.is_file():
                return _site_html(not_found, name, status_code=404)
            raise HTTPException(404, f"no such site path: {rel_path}")
    if target.suffix == ".html":
        return _site_html(target, name)
    return FileResponse(target)


def _site_html(path: Path, name: str, status_code: int = 200) -> Response:
    """Serve a Quartz page with its basepath re-aimed at this route.

    The build bakes the PUBLIC mount path (basepath="/<name>", from the
    config baseUrl) into every page; client-side navigation builds URLs
    from it, so links break when the same build is viewed here. Rewriting
    the attribute per response keeps one build correct in both places —
    the files on disk stay exactly what the public host needs.
    """
    html = path.read_text(encoding="utf-8")
    html = html.replace(f'basepath="/{name}"', f'basepath="/api/kb/{name}/site"')
    return Response(html, status_code=status_code, media_type="text/html")


# --------------------------------------------------------------- server

@app.get("/api/server/slots")
async def get_slots(endpoint: str = config.DEFAULT_ENDPOINT):
    """LLM busy light: proxy llama.cpp's /slots on the chosen host."""
    if endpoint not in config.ENDPOINTS:
        raise HTTPException(400, f"unknown endpoint: {endpoint!r}")
    # Endpoints with an API key are hosted services (OpenRouter etc.) —
    # no llama.cpp /slots to probe, and no queue worth a busy light.
    if endpoint in config.ENDPOINT_KEYS:
        return {"endpoint": endpoint, "hosted": True,
                "total": 0, "busy": 0, "idle": 0}
    base = config.ENDPOINTS[endpoint].removesuffix("/v1")
    # Some hosts front llama.cpp with a model router that 400s without an
    # explicit model name; llama.cpp itself ignores the extra param.
    model = config.endpoint_model(endpoint).split("/", 1)[-1]
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{base}/slots", params={"model": model})
            resp.raise_for_status()
            slots = resp.json()
    except Exception as e:
        return JSONResponse(
            {"endpoint": endpoint, "error": str(e)}, status_code=502
        )
    busy = sum(1 for s in slots if s.get("is_processing"))
    return {
        "endpoint": endpoint,
        "total": len(slots),
        "busy": busy,
        "idle": len(slots) - busy,
    }


# ------------------------------------------------------------------ mcp
# MCP over streamable HTTP for external clients (Claude Code/Desktop):
#   claude mcp add --transport http openkb http://openkb.local/mcp

app.mount("/mcp", mcp_server.mcp.streamable_http_app())


@app.middleware("http")
async def _mcp_no_slash(request: Request, call_next):
    """Clients POST to /mcp (no slash); the mount answers at /mcp/."""
    if request.scope["path"] == "/mcp":
        request.scope["path"] = "/mcp/"
        request.scope["raw_path"] = b"/mcp/"
    return await call_next(request)


# --------------------------------------------------------------- static
# Mounted last so /api/* wins; in production Apache serves static/ and
# only /api/ reaches uvicorn.

from fastapi.staticfiles import StaticFiles  # noqa: E402

STATIC_DIR = config.WEBUI_DIR / "static"
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
