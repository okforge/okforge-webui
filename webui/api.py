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
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import (FastAPI, Form, HTTPException, Request, Response,
                     UploadFile)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse)
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


def _to_trash(src: Path, kind: str) -> Path:
    """Archive-first delete: MOVE src into TRASH_DIR/<kind>/ (created on
    demand). Name collisions get a timestamp suffix so nothing is ever
    overwritten; emptying the trash is a manual act outside the UI."""
    dest_dir = config.TRASH_DIR / kind
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        dest = dest_dir / f"{src.name}.{int(time.time())}"
    shutil.move(str(src), str(dest))
    return dest


def _active_jobs() -> list[dict]:
    return jobs.list_jobs(limit=1000, active=True)


def _project_busy(name: str) -> bool:
    """Queued/running work touching a project (its KB or its md-out)."""
    return any(j["kb"] == name or j["params"].get("out_name") == name
               for j in _active_jobs())


@app.delete("/api/inbox/{name}")
def delete_pdf(name: str):
    """Move an inbox PDF to trash/inbox/. Refused while any queued or
    running job references it — the OCR would fail mid-run."""
    p = config.INBOX_DIR / Path(name).name
    if not p.is_file() or p.suffix.lower() != ".pdf":
        raise HTTPException(404, f"no such inbox PDF: {name}")
    if any(j["params"].get("pdf") == str(p) for j in _active_jobs()):
        raise HTTPException(409, f"{p.name} has queued/running jobs — "
                                 "wait for them or cancel them first")
    trashed = _to_trash(p, "inbox")
    # Its job history goes with it — a re-upload of the same name must
    # not resurface the old runs in the queue.
    purged = jobs.purge_jobs_for_pdf(str(p))
    return {"name": p.name, "trashed_to": str(trashed), "purged_jobs": purged}


# Image scans are welcome too: wrapped into a one-page PDF at upload so
# the whole probe→pilot→OCR pipeline runs unchanged (the engine itself
# takes no images — OCR is the only road in for them).
_IMAGE_UPLOAD_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}


@app.post("/api/inbox")
async def upload_pdf(file: UploadFile):
    name = Path(file.filename or "").name
    suffix = Path(name).suffix.lower()
    if suffix != ".pdf" and suffix not in _IMAGE_UPLOAD_EXTS:
        raise HTTPException(400, "only .pdf uploads (or page-scan images: "
                                 "jpg / png / tif / bmp) are accepted")
    is_image = suffix != ".pdf"
    dest = config.INBOX_DIR / (Path(name).stem + ".pdf" if is_image else name)
    if dest.exists():
        raise HTTPException(409, f"{dest.name} already exists in the inbox")
    tmp = dest.with_name(dest.name + ".part") if is_image else dest
    try:
        with tmp.open("wb") as out:
            while chunk := await file.read(1 << 20):
                out.write(chunk)
        if is_image:
            import pymupdf
            try:
                with pymupdf.open(str(tmp)) as img:
                    pdf_bytes = img.convert_to_pdf()
            except Exception:
                raise HTTPException(400, f"{name}: not a readable image")
            dest.write_bytes(pdf_bytes)
    except Exception:
        dest.unlink(missing_ok=True)
        if is_image:
            tmp.unlink(missing_ok=True)
        raise
    if is_image:
        tmp.unlink(missing_ok=True)
    st = dest.stat()
    return {"name": dest.name, "path": str(dest), "size": st.st_size}


@app.post("/api/inbox/combine", status_code=201)
async def combine_upload(files: list[UploadFile], name: str = Form(...)):
    """Combine several PDFs / page-scan images into ONE inbox PDF, in
    the order the files were sent (the UI natural-sorts by filename and
    shows the plan before uploading). Output is an ordinary inbox PDF —
    the pipeline neither knows nor cares that it was combined. Nothing
    is written until every part has merged cleanly."""
    if len(files) < 2:
        raise HTTPException(400, "combine needs at least two files")
    stem = jobs._safe_stem(name.strip()) if name.strip() else ""
    if not stem:
        raise HTTPException(400, "a name for the combined PDF is required")
    dest = config.INBOX_DIR / f"{stem}.pdf"
    if dest.exists():
        raise HTTPException(409, f"{dest.name} already exists in the inbox")
    for f in files:
        src = Path(f.filename or "").name
        suffix = Path(src).suffix.lower()
        if suffix != ".pdf" and suffix not in _IMAGE_UPLOAD_EXTS:
            raise HTTPException(400, f"{src or '(unnamed)'}: only PDFs and "
                                     "page-scan images (jpg / png / tif / bmp) "
                                     "can be combined")
    import pymupdf
    merged = pymupdf.open()
    parts = []
    try:
        with tempfile.TemporaryDirectory(prefix="okforge-combine-") as td:
            for i, f in enumerate(files):
                src = Path(f.filename or "").name
                # keep the extension: pymupdf picks the parser by it
                tmp = Path(td) / f"part{i}{Path(src).suffix.lower()}"
                with tmp.open("wb") as out:
                    while chunk := await f.read(1 << 20):
                        out.write(chunk)
                start = merged.page_count + 1
                try:
                    with pymupdf.open(str(tmp)) as doc:
                        if tmp.suffix == ".pdf":
                            merged.insert_pdf(doc)
                        else:
                            pdf_bytes = doc.convert_to_pdf()
                            with pymupdf.open(stream=pdf_bytes,
                                              filetype="pdf") as one:
                                merged.insert_pdf(one)
                except Exception:
                    raise HTTPException(400,
                                        f"{src}: not a readable PDF/image")
                end = merged.page_count
                parts.append({"name": src, "pages": str(start) if start == end
                              else f"{start}-{end}"})
            total = merged.page_count
            merged.save(str(dest))
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    finally:
        merged.close()
    st = dest.stat()
    return {"name": dest.name, "path": str(dest), "size": st.st_size,
            "page_count": total, "parts": parts}


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
    dest: str = "kb"  # full jobs: "kb" (ingest) or "md-out" (markdown only)
    out_name: str | None = None  # md-out run name (dir under MD_OUT_DIR)
    src: str | None = None  # ingest_md jobs: "kb" (raw/) or "md-out"
    auto_ingest: bool = False  # md-out full jobs: queue ingest_md at the end
    create_kb: bool = False  # ingest_md jobs: init the KB if it doesn't exist
    lang: str | None = None  # KB language when create_kb/auto_ingest inits one


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
    if req.dest not in ("kb", "md-out"):
        raise ValueError(f"unknown dest: {req.dest!r}")
    if req.type == "full" and req.dest == "md-out":
        # KB-less markdown-only run: chunks land in MD_OUT_DIR/<out_name>/.
        if req.kb:
            raise ValueError("an md-out run takes no kb")
        if req.endpoint and req.endpoint not in config.ENDPOINTS:
            raise ValueError(f"unknown endpoint: {req.endpoint!r}")
        params["dest"] = "md-out"
        params["out_name"] = jobs._safe_stem(
            req.out_name or Path(params["pdf"]).stem)
        if req.auto_ingest:
            # Fail now, not when the auto-queued ingest tries to init.
            if not kb.KB_NAME_RE.match(params["out_name"]):
                raise ValueError(
                    "auto-ingest needs a KB-valid project name "
                    f"(letter first, then letters/digits/_-): {params['out_name']!r}")
            params["auto_ingest"] = True
            params["lang"] = req.lang
        return params
    if req.type == "ingest_md":
        # Decoupled ingest: add already-produced chunk md into a KB —
        # from an md-out run (copied into raw/ first) or from raw/ itself.
        if not req.kb:
            raise ValueError("ingest_md job requires a kb")
        if req.create_kb:
            # KB may not exist yet — the runner inits it (decoupled flow).
            if not kb.KB_NAME_RE.match(req.kb):
                raise ValueError(f"invalid KB name: {req.kb!r}")
            if req.endpoint and req.endpoint not in config.ENDPOINTS:
                raise ValueError(f"unknown endpoint: {req.endpoint!r}")
            params["create_kb"] = True
            params["lang"] = req.lang
        else:
            kb.resolve_kb(req.kb)
        src = req.src or "kb"
        if src not in ("kb", "md-out"):
            raise ValueError(f"unknown src: {src!r}")
        if src == "md-out":
            if not req.out_name:
                raise ValueError("ingest_md from md-out requires out_name")
            params["out_name"] = jobs._safe_stem(req.out_name)
            if not (config.MD_OUT_DIR / params["out_name"]).is_dir():
                raise ValueError(f"no such md-out run: {req.out_name}")
        params["src"] = src
        return params
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
def get_jobs(limit: int = 100, active: bool = False):
    return {"jobs": jobs.list_jobs(limit=limit, active=active)}


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


def _tree_index_html(title: str, url_prefix: str, files: list[dict],
                     note: str = "", extra_html: str = "") -> str:
    """Browser view of a raw/ or md-out listing: download links per file.
    Pipelines never see this — they get JSON (content negotiation in
    _serve_tree)."""
    import html as _html

    rows = []
    for f in files:
        href = url_prefix + "/".join(quote(s) for s in f["path"].split("/"))
        is_img = Path(f["path"]).suffix.lower() in _WIKI_IMAGE_TYPES
        rows.append(
            f'<tr><td><a href="{href}"{"" if is_img else " download"}>'
            f'{_html.escape(f["path"])}</a></td>'
            f'<td class="n">{f["size"]:,}</td></tr>'
        )
    where = _html.escape(title)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{where}</title><style>
body {{ font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 50rem; padding: 0 1rem; }}
table {{ border-collapse: collapse; width: 100%; font-size: .9rem; }}
td {{ padding: .25rem .6rem; border-bottom: 1px solid #ddd; }}
td.n {{ text-align: right; color: #777; white-space: nowrap; }}
.note {{ color: #777; font-size: .85rem; }}
</style></head><body>
<h1>{where}</h1>
<p class="note">{note}</p>
{extra_html}
<table>{"".join(rows)}</table>
</body></html>"""


def _serve_tree(base: Path, rel_path: str, request: Request, *,
                title: str, url_prefix: str, note: str = "",
                extra_html: str = "", json_extra: dict | None = None):
    """Shared read-only tree browser (KB raw/ and md-out runs).
    Directories return a flat RECURSIVE listing with size + mtime — one
    call enumerates everything a pipeline needs to sync — as JSON, or as
    a human download page when the client prefers text/html (a browser).
    Files are served directly. Hidden entries are skipped."""
    target = (base / rel_path).resolve() if rel_path else base
    if not target.is_relative_to(base) or not target.exists():
        raise HTTPException(404, f"no such path: {rel_path}")
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
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(
                _tree_index_html(title, url_prefix, files, note, extra_html))
        return {**(json_extra or {}), "path": rel_path, "files": files}
    suffix = target.suffix.lower()
    if suffix in _RAW_BINARY_TYPES:
        return FileResponse(target, media_type=_RAW_BINARY_TYPES[suffix])
    if suffix in _WIKI_TEXT_EXTS:
        return PlainTextResponse(target.read_text(encoding="utf-8"))
    raise HTTPException(404, f"unsupported file type: {rel_path}")


@app.get("/api/kb/{name}/raw")
@app.get("/api/kb/{name}/raw/{rel_path:path}")
def get_raw(name: str, request: Request, rel_path: str = ""):
    """Read-only access to raw/ — the pristine ingested sources
    (<stem>_pN_M.md chunks, .pages.json page arrays, _images/ crops,
    archived PDFs) so external pipelines can pull them (ROADMAP P12:
    feed a separate RAG). Hidden entries (.reocr_job* scratch dirs) are
    skipped."""
    try:
        kb_dir = kb.resolve_kb(name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    base = (kb_dir / "raw").resolve()
    if not base.is_dir():
        raise HTTPException(404, f"{name} has no raw/ yet")
    kb_url = f"/api/kb/{quote(name)}"
    return _serve_tree(
        base, rel_path, request,
        title=f"{name}/raw/{rel_path}".rstrip("/"),
        url_prefix=f"{kb_url}/raw/",
        note="Pristine ingested sources (OCR'd markdown chunks, page JSON,\n"
             "image crops, archived PDFs). Click to download; images open for\n"
             "viewing. Pipelines should request this URL with\n"
             "<code>Accept: application/json</code> (curl's default) for a\n"
             "machine-readable listing.",
        extra_html=f'<p><a href="{kb_url}/sources.md"><b>Download all ingested '
                   'chunks as one\nmarkdown file</b></a> <span class="note">'
                   '(page order — for re-chunking in a\nvector RAG)</span></p>',
        json_extra={"kb": name},
    )


# md-out run names come out of jobs._safe_stem, so this is the full alphabet.
_MD_RUN_RE = re.compile(r"^[\w\-]+$")


class MdOutCreate(BaseModel):
    name: str


@app.post("/api/md-out", status_code=201)
def create_md_out(req: MdOutCreate):
    """Create an empty markdown-only output, so it can be picked as the
    stage-3 output before its first run fills it."""
    name = jobs._safe_stem(req.name.strip()) if req.name.strip() else ""
    if not name:
        raise HTTPException(400, "output name required")
    d = config.MD_OUT_DIR / name
    if d.is_dir():
        raise HTTPException(409, f"markdown output {name} already exists")
    d.mkdir(parents=True)
    return {"name": name}


_MD_UPLOAD_EXTS = {".md", ".markdown", ".txt"}


@app.post("/api/md-out/{name}/files", status_code=201)
async def add_md_files(name: str, files: list[UploadFile]):
    """Add hand-made markdown/text documents to a project — the no-OCR
    input path. Saved as <safe-stem>.md in md-out/<name>/, where the
    normal ingest flow picks them up like any OCR'd chunk. All files
    are validated before any is written, so a rejected batch changes
    nothing."""
    if not _MD_RUN_RE.match(name):
        raise HTTPException(404, f"no such project: {name}")
    dest_dir = config.MD_OUT_DIR / name
    plan = []
    seen = set()
    for f in files:
        src = Path(f.filename or "").name
        if Path(src).suffix.lower() not in _MD_UPLOAD_EXTS:
            raise HTTPException(400, f"{src or '(unnamed)'}: only "
                                     ".md / .markdown / .txt files are accepted "
                                     "— pre-convert other formats to markdown")
        stem = jobs._safe_stem(Path(src).stem)
        dest = dest_dir / f"{stem}.md"
        if dest.exists() or stem in seen:
            raise HTTPException(409, f"{stem}.md already exists in project "
                                     f"{name} — delete the project's markdown "
                                     "or rename the file")
        seen.add(stem)
        plan.append((f, dest))
    # KB-only projects (created before md-out existed, or by the engine
    # CLI) have no markdown dir yet — first upload creates it.
    dest_dir.mkdir(parents=True, exist_ok=True)
    added = []
    for f, dest in plan:
        try:
            with dest.open("wb") as out:
                while chunk := await f.read(1 << 20):
                    out.write(chunk)
        except Exception:
            dest.unlink(missing_ok=True)
            raise
        added.append(dest.name)
    return {"added": added}


@app.get("/api/md-out")
def get_md_out():
    """Standalone OCR runs (md-out mode): one dir per run under
    MD_OUT_DIR, produced by KB-less `full` jobs with dest="md-out"."""
    runs = []
    if config.MD_OUT_DIR.is_dir():
        for d in sorted(config.MD_OUT_DIR.iterdir(), key=lambda d: d.name.lower()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            chunks = sum(1 for p in d.glob("*.md") if not p.stem.endswith("_src"))
            files = sum(1 for p in d.rglob("*") if p.is_file())
            runs.append({"name": d.name, "chunks": chunks, "files": files,
                         "mtime": int(d.stat().st_mtime)})
    return {"md_out_dir": str(config.MD_OUT_DIR), "runs": runs}


@app.delete("/api/md-out/{name}")
def delete_md_out(name: str):
    """Move a project's markdown dir to trash/md-out/ (redo-the-OCR
    path). Refused while the project has queued/running jobs."""
    base = (config.MD_OUT_DIR / name).resolve()
    if (not _MD_RUN_RE.match(name) or not base.is_dir()
            or not base.is_relative_to(config.MD_OUT_DIR.resolve())):
        raise HTTPException(404, f"no such markdown output: {name}")
    if _project_busy(name):
        raise HTTPException(409, f"project {name} has queued/running jobs — "
                                 "wait for them or cancel them first")
    return {"name": name, "trashed_to": str(_to_trash(base, "md-out"))}


@app.get("/api/md-out/{name}")
@app.get("/api/md-out/{name}/{rel_path:path}")
def get_md_out_run(name: str, request: Request, rel_path: str = ""):
    """Read-only access to one md-out run's artifacts — same browsing
    contract as a KB's raw/ (JSON listing or a human download page)."""
    base = (config.MD_OUT_DIR / name).resolve()
    if (not _MD_RUN_RE.match(name) or not base.is_dir()
            or not base.is_relative_to(config.MD_OUT_DIR.resolve())):
        raise HTTPException(404, f"no such md-out run: {name}")
    return _serve_tree(
        base, rel_path, request,
        title=f"md-out/{name}/{rel_path}".rstrip("/"),
        url_prefix=f"/api/md-out/{quote(name)}/",
        note="Standalone OCR output (markdown chunks, page JSON, image\n"
             "crops) — not ingested into any KB. Click to download; images\n"
             "open for viewing. Pipelines should request this URL with\n"
             "<code>Accept: application/json</code> (curl's default) for a\n"
             "machine-readable listing.",
        json_extra={"run": name},
    )


@app.delete("/api/kb/{name}/site")
def delete_site(name: str):
    """Move a KB's published static site to trash/sites/. The KB's
    `published` flag self-heals — it's derived from the sites dir."""
    try:
        kb.resolve_kb(name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    site = (config.SITES_DIR / name).resolve()
    if not site.is_dir() or not site.is_relative_to(config.SITES_DIR.resolve()):
        raise HTTPException(404, f"{name} has no published site")
    if _project_busy(name):
        raise HTTPException(409, f"project {name} has queued/running jobs — "
                                 "wait for them or cancel them first")
    return {"name": name, "trashed_to": str(_to_trash(site, "sites"))}


@app.get("/api/kb/{name}/docs")
def kb_docs(name: str):
    """Indexed doc stems (the engine's own list) — lets the UI tell
    which markdown chunks are not yet ingested."""
    try:
        kb_dir = kb.resolve_kb(name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    try:
        return {"docs": sorted(jobs._indexed_stems(kb_dir))}
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.get("/api/kb/{name}/sources.md")
def get_sources_md(name: str):
    """All INGESTED chunks concatenated in page order, one markdown
    download — for RAG systems that re-chunk anyway. Built from the
    engine's own indexed-doc list, so source-language variants (_src,
    legacy _ca/_es), pilots, and never-added leftovers are excluded."""
    try:
        kb_dir = kb.resolve_kb(name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    try:
        stems = jobs._indexed_stems(kb_dir)
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    parts = []
    for stem in sorted(stems, key=jobs._page_key):
        p = kb_dir / "raw" / f"{stem}.md"
        if p.is_file():
            parts.append(f"<!-- source: {stem}.md -->\n\n"
                         + p.read_text(encoding="utf-8").strip())
    if not parts:
        raise HTTPException(404, f"{name} has no ingested raw sources")
    return PlainTextResponse(
        "\n\n".join(parts) + "\n",
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{name}-sources.md"'},
    )


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
