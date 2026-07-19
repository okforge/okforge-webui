"""Serial job queue: one worker thread, sqlite state, per-job log files.

The single worker is the design's load-bearing wall (PLAN.md): it
structurally enforces "never two `openkb add`s at once" and "never
concurrent OCR runs against the 2-slot llama.cpp server". Jobs survive a
backend restart: anything found 'running' at startup was orphaned by a
crash and is re-queued (OCR re-runs are cheap and idempotent; the add
runner checks `openkb list` before adding).
"""

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from . import config
from . import kb as kbmod
from . import probe

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    type     TEXT NOT NULL,
    kb       TEXT,
    params   TEXT NOT NULL,
    status   TEXT NOT NULL DEFAULT 'queued',
    created  REAL NOT NULL,
    started  REAL,
    finished REAL,
    error    TEXT,
    parent   INTEGER,
    pid      INTEGER
);
"""

JOB_TYPES = {"pilot", "ocr", "translate", "add", "full", "reocr", "extract",
             "publish", "recompile", "ingest_md", "describe"}
TERMINAL = {"done", "failed", "cancelled"}

# A running job whose log has been silent this long is flagged as stalled
# in the UI (flag only — never auto-killed; a long `add` can be quiet
# while the engine compiles concepts).
STALL_SECONDS = 20 * 60

_db_lock = threading.Lock()
_worker_started = False
_current = {"job_id": None, "proc": None}  # what the worker is running now
_current_lock = threading.Lock()

# Killing a job must take out everything it spawned. POSIX: children get
# their own session at spawn so killpg reaches the whole tree. Windows:
# no sessions/process groups in the POSIX sense — spawn with a new
# process group and let taskkill /T walk the tree.
if os.name == "nt":
    _POPEN_GROUP_KWARGS = {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
    }
else:
    _POPEN_GROUP_KWARGS = {"start_new_session": True}


def _terminate_tree(pid: int) -> None:
    """Best-effort kill of a job process and everything it spawned."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
            )
        else:
            os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(config.JOBS_DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _init_db() -> None:
    config.JOB_LOG_DIR.mkdir(exist_ok=True)
    with _db_lock, _conn() as c:
        c.executescript(_SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
        if "pid" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN pid INTEGER")


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["params"] = json.loads(d["params"])
    return d


def log_path(job_id: int) -> Path:
    return config.JOB_LOG_DIR / f"job_{job_id}.log"


def pilot_dir(job_id: int) -> Path:
    return config.WEBUI_DIR / "pilot" / f"job_{job_id}"


def job_files_dir(job: dict) -> Path:
    """Base dir the /files/ endpoint serves a job's artifacts from —
    where its out_md and image crops land."""
    if job["type"] == "pilot":
        return pilot_dir(job["id"])
    if job["kb"]:
        return kbmod.resolve_kb(job["kb"]) / "raw"
    return md_out_dir(job["params"]["out_name"])


# ------------------------------------------------------------- queue API

def enqueue(type_: str, params: dict, kb_name: str | None = None,
            parent: int | None = None) -> dict:
    if type_ not in JOB_TYPES:
        raise ValueError(f"unknown job type: {type_!r}")
    with _db_lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO jobs (type, kb, params, status, created, parent) "
            "VALUES (?, ?, ?, 'queued', ?, ?)",
            (type_, kb_name, json.dumps(params), time.time(), parent),
        )
        job_id = cur.lastrowid
    return get_job(job_id)


def get_job(job_id: int) -> dict | None:
    with _db_lock, _conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _annotate(_row_to_dict(row)) if row else None


def list_jobs(limit: int = 100, active: bool = False) -> list[dict]:
    """Newest jobs first. active=True returns only queued/running rows —
    the set duplicate-run guards and status views need, immune to a busy
    run flooding the newest-N window (a 1-page-chunk book is ~400 rows)."""
    q = "SELECT * FROM jobs "
    if active:
        q += "WHERE status IN ('queued', 'running') "
    q += "ORDER BY id DESC LIMIT ?"
    with _db_lock, _conn() as c:
        rows = c.execute(q, (limit,)).fetchall()
    return [_annotate(_row_to_dict(r)) for r in rows]


def active_count_for_kb(kb_name: str) -> int:
    """Queued + running jobs bound to a KB — the retire-KB guard."""
    with _db_lock, _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM jobs WHERE kb = ? "
            "AND status IN ('queued', 'running')",
            (kb_name,),
        ).fetchone()
    return row[0]


def _chunk_page_count(params: dict) -> int | None:
    """Pages covered by a job's pages spec ("7-26" → 20, "9" → 1)."""
    pages = params.get("pages")
    if not pages or not re.match(r"^\d+(-\d+)?$", str(pages)):
        return None
    a, _, b = str(pages).partition("-")
    return int(b or a) - int(a) + 1


def seconds_per_page(type_: str) -> float | None:
    """Average seconds/page for a job type over recent completed runs."""
    with _db_lock, _conn() as c:
        rows = c.execute(
            "SELECT params, started, finished FROM jobs "
            "WHERE type = ? AND status = 'done' AND started IS NOT NULL "
            "AND finished IS NOT NULL ORDER BY id DESC LIMIT 40",
            (type_,),
        ).fetchall()
    rates = []
    for r in rows:
        pages = _chunk_page_count(json.loads(r["params"]))
        duration = (r["finished"] or 0) - (r["started"] or 0)
        if pages and duration > 1:
            rates.append(duration / pages)
    if not rates:
        return None
    return sum(rates) / len(rates)


def _annotate(job: dict) -> dict:
    """Attach liveness/ETA fields to a running job (stall flag + estimate)."""
    if job["status"] != "running":
        return job
    lp = log_path(job["id"])
    try:
        last_activity = lp.stat().st_mtime
    except OSError:
        last_activity = job.get("started") or time.time()
    job["log_idle_seconds"] = int(time.time() - last_activity)
    job["stalled"] = job["log_idle_seconds"] >= STALL_SECONDS
    pages = _chunk_page_count(job["params"])
    rate = seconds_per_page(job["type"]) if pages else None
    if pages and rate:
        elapsed = time.time() - (job.get("started") or time.time())
        job["eta_seconds"] = max(0, int(pages * rate - elapsed))
    else:
        job["eta_seconds"] = None
    return job


def retry(job_id: int) -> dict:
    """Re-enqueue a terminal job with identical params (one-click retry).

    A re-queued `full` job re-expands with the same range + chunk size and
    skips chunks already indexed or artifacts already on disk — that is the
    Resume path. A child step re-runs just itself, grouped under the same
    parent so the UI keeps it with its run.
    """
    job = get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if job["status"] not in TERMINAL:
        raise ValueError(f"job {job_id} is {job['status']}; only finished jobs retry")
    new = enqueue(
        job["type"], {**job["params"], "retry_of": job_id},
        kb_name=job["kb"], parent=job["parent"],
    )
    # Cross-link the old row so the UI can say "retried as #N" instead of
    # leaving a terminal row that reads like lost work.
    old_params = {**job["params"], "retried_as": new["id"]}
    with _db_lock, _conn() as c:
        c.execute("UPDATE jobs SET params = ? WHERE id = ?",
                  (json.dumps(old_params), job_id))
    return new


def purge_jobs_for_pdf(pdf_path: str) -> int:
    """Delete terminal job rows (and their children + log files) that
    reference this source PDF — called when the PDF itself is deleted,
    so a later re-upload of the same name starts with a clean history
    instead of resurfacing the old runs. Active jobs are the caller's
    problem (the delete endpoint refuses while any exist)."""
    with _db_lock, _conn() as c:
        rows = c.execute(
            "SELECT id FROM jobs WHERE status IN ('done','failed','cancelled') "
            "AND json_extract(params, '$.pdf') = ?", (pdf_path,)).fetchall()
        ids = {r[0] for r in rows}
        if ids:
            marks = ",".join("?" * len(ids))
            kid_rows = c.execute(
                f"SELECT id FROM jobs WHERE parent IN ({marks}) "
                "AND status IN ('done','failed','cancelled')",
                tuple(ids)).fetchall()
            ids |= {r[0] for r in kid_rows}
            marks = ",".join("?" * len(ids))
            c.execute(f"DELETE FROM jobs WHERE id IN ({marks})", tuple(ids))
    for jid in ids:
        log_path(jid).unlink(missing_ok=True)
    return len(ids)


def children(parent_id: int) -> list[dict]:
    with _db_lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM jobs WHERE parent = ? ORDER BY id", (parent_id,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _set_status(job_id: int, status: str, error: str | None = None) -> None:
    field = "started" if status == "running" else "finished"
    with _db_lock, _conn() as c:
        if status in TERMINAL or status == "running":
            c.execute(
                f"UPDATE jobs SET status = ?, error = ?, {field} = ? WHERE id = ?",
                (status, error, time.time(), job_id),
            )
        else:
            c.execute(
                "UPDATE jobs SET status = ?, error = ? WHERE id = ?",
                (status, error, job_id),
            )


def cancel(job_id: int) -> dict:
    job = get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if job["status"] == "queued":
        _set_status(job_id, "cancelled")
        # cancel queued children of a full job too
        for ch in children(job_id):
            if ch["status"] == "queued":
                _set_status(ch["id"], "cancelled")
    elif job["status"] == "running":
        with _current_lock:
            proc = _current["proc"] if _current["job_id"] == job_id else None
        if proc is not None and proc.poll() is None:
            _terminate_tree(proc.pid)
        _set_status(job_id, "cancelled")
    return get_job(job_id)


# ----------------------------------------------------------- job runners

def _run_logged(job_id: int, cmd: list[str], cwd: Path,
                env_extra: dict | None = None) -> int:
    """Run cmd appending merged stdout+stderr to the job log; return rc."""
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    with log_path(job_id).open("a", encoding="utf-8") as log:
        log.write(f"$ {' '.join(cmd)}\n")
        log.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,  # any interactive prompt fails fast
            stdout=log,
            stderr=subprocess.STDOUT,
            # own session/group so cancel can kill the whole tree
            **_POPEN_GROUP_KWARGS,
        )
        with _db_lock, _conn() as c:
            c.execute("UPDATE jobs SET pid = ? WHERE id = ?", (proc.pid, job_id))
        with _current_lock:
            _current["proc"] = proc
        try:
            return proc.wait()
        finally:
            with _current_lock:
                _current["proc"] = None


def _endpoint_env(params: dict) -> dict:
    """OPENAI_API_BASE override for jobs that run outside a KB dir."""
    label = params.get("endpoint") or config.DEFAULT_ENDPOINT
    if label not in config.ENDPOINTS:
        raise ValueError(f"unknown endpoint: {label!r}")
    # The OCR/translate tools speak raw OpenAI protocol, so they get the
    # model name without its LiteLLM provider prefix (openai/X -> X,
    # openrouter/qwen/Y -> qwen/Y).
    return {"OPENAI_API_BASE": config.ENDPOINTS[label],
            "LLM_API_KEY": config.endpoint_key(label),
            "OKFORGE_VISION_MODEL": config.endpoint_model(label).split("/", 1)[-1]}


def _kb_vision_env(kb_dir: Path) -> dict:
    """Model override for OCR/translate runs inside a KB dir. The KB's
    .env (via cwd) already supplies base URL and key; the model only
    needs overriding when the KB's endpoint declares its own (hosted
    services use different model names than the local default)."""
    label = kbmod.endpoint_label(kb_dir)
    if label is None or label not in config.ENDPOINT_MODELS:
        return {}
    return {"OKFORGE_VISION_MODEL":
            config.ENDPOINT_MODELS[label].split("/", 1)[-1]}


def _pages_spec(params: dict) -> str | None:
    pages = params.get("pages")
    if pages in (None, ""):
        return None
    if not re.match(r"^\d+(-\d+)?$", str(pages)):
        raise ValueError(f"bad pages spec: {pages!r}")
    return str(pages)


def _ocr_cmd(pdf: Path, out_md: Path, pages: str | None, figures: bool,
             tables: bool = False, prompt_extra: str | None = None) -> list[str]:
    cmd = [str(config.OCR_BIN), str(pdf), str(out_md)]
    if pages:
        cmd += ["--pages", pages]
    if figures:
        cmd.append("--figures")
    if tables:
        # Table mode: reasoning on + table-reconstruction prompt — for
        # pages whose complex tables the fast path mangles.
        cmd += ["--think", "--tables"]
    if prompt_extra:
        # Per-document escape hatch: free-text instructions appended to
        # the OCR prompt ("ignore marginalia", "columns read right-to-left").
        cmd += ["--prompt-extra", prompt_extra]
    return cmd


def _run_pilot(job: dict) -> None:
    """OCR 1–3 pages into a webui-owned scratch dir. Runs before any KB
    exists, so the endpoint comes from params, not a KB .env."""
    params = job["params"]
    pdf = probe.allowed_pdf_path(params["pdf"])
    pages = _pages_spec(params)
    if pages is None:
        raise ValueError("pilot requires a pages spec")
    out_dir = pilot_dir(job["id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = _ocr_cmd(pdf, out_dir / "pilot.md", pages, bool(params.get("figures")),
                   tables=bool(params.get("tables")),
                   prompt_extra=params.get("prompt_extra"))
    rc = _run_logged(job["id"], cmd, cwd=out_dir, env_extra=_endpoint_env(params))
    if rc != 0:
        raise RuntimeError(f"okforge-vision-ocr exited {rc}")


_STEM_SAFE_RE = re.compile(r"[^\w\-]+")


def _page_key(stem: str):
    """Sort key putting chunk stems in page order (foo_p21_40 after
    foo_p1_20); non-chunk stems sort after, alphabetically."""
    m = re.search(r"_p(\d+)", stem)
    return (0, int(m.group(1)), stem) if m else (1, 0, stem.lower())


def _safe_stem(stem: str) -> str:
    """Engine-compatible stem sanitisation (mirrors converter._sanitize_stem)
    so chunk artifacts and the engine's doc names coincide — a PDF stem with
    a space used to yield TWO names for one doc ("Building _Code" artifacts
    vs "Building-_Code" registry), which broke the remove step live."""
    import unicodedata

    return _STEM_SAFE_RE.sub("-", unicodedata.normalize("NFKC", stem)).strip("-") or "document"


def md_out_dir(out_name: str) -> Path:
    """A standalone OCR run's output dir under MD_OUT_DIR (md-out mode)."""
    return config.MD_OUT_DIR / _safe_stem(out_name)


def _artifact_dir(job: dict) -> Path:
    """Where a chunk job's .md/.pages.json/_images land: the KB's raw/
    for KB-bound jobs, MD_OUT_DIR/<out_name>/ for KB-less md-out jobs."""
    if job["kb"]:
        return kbmod.resolve_kb(job["kb"]) / "raw"
    return md_out_dir(job["params"]["out_name"])


def ocr_out_md(out_dir: Path, pdf: Path, pages: str | None,
               translate: bool = False) -> Path:
    """Chunk naming per SeminoleWars: <out_dir>/<stem>_p<start>_<end>.md;
    whole doc keeps the bare stem. Translated chunks get an _en suffix
    later. out_dir is a KB's raw/ or an md-out run dir.

    Stems are sanitised for new artifacts; when a legacy (unsanitised)
    file already exists it wins, so resume/re-OCR keep working on KBs
    ingested before the flattening."""
    if pages:
        start, _, end = pages.partition("-")
        suffix = f"_p{start}_{end or start}"
    else:
        suffix = ""
    lang_suffix = "_src" if translate else ""
    sanitized = out_dir / f"{_safe_stem(pdf.stem)}{suffix}{lang_suffix}.md"
    legacy = out_dir / f"{pdf.stem}{suffix}{lang_suffix}.md"
    if legacy != sanitized and legacy.exists() and not sanitized.exists():
        return legacy
    return sanitized


def _job_cwd_env(job: dict, out_dir: Path) -> tuple[Path, dict]:
    """cwd + env for OCR/translate runs: inside a KB, cwd = KB dir so its
    .env picks the LLM endpoint; KB-less md-out runs take the endpoint
    from params instead (same pattern as pilots)."""
    if job["kb"]:
        kb_dir = kbmod.resolve_kb(job["kb"])
        return kb_dir, _kb_vision_env(kb_dir)
    return out_dir, _endpoint_env(job["params"])


def _run_ocr(job: dict) -> None:
    """OCR a chunk into the KB's raw/ dir, or into the md-out run dir
    for KB-less runs."""
    params = job["params"]
    pdf = probe.allowed_pdf_path(params["pdf"])
    pages = _pages_spec(params)
    out_dir = _artifact_dir(job)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_md = ocr_out_md(out_dir, pdf, pages, bool(params.get("translate")))
    cmd = _ocr_cmd(pdf, out_md, pages, bool(params.get("figures")),
                   prompt_extra=params.get("prompt_extra"))
    cwd, env = _job_cwd_env(job, out_dir)
    rc = _run_logged(job["id"], cmd, cwd=cwd, env_extra=env)
    if rc != 0:
        raise RuntimeError(f"okforge-vision-ocr exited {rc}")


def _indexed_stems(kb_dir: Path) -> set[str]:
    """Doc stems already in the KB, from `openkb list --json` (okforge's
    machine interface — full names, no table truncation)."""
    proc = subprocess.run(
        [str(config.OPENKB_BIN), "--kb-dir", str(kb_dir), "list", "--json"],
        cwd=kb_dir,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"openkb list failed: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    return set(data.get("summaries", []))


def _log_line(job_id: int, text: str) -> None:
    with log_path(job_id).open("a", encoding="utf-8") as log:
        log.write(text + "\n")


def _run_translate(job: dict) -> None:
    """Translate an OCR'd chunk (<stem>_src.pages.json) to English,
    writing the final <stem>.md next to it so image refs keep resolving."""
    params = job["params"]
    pdf = probe.allowed_pdf_path(params["pdf"])
    pages = _pages_spec(params)
    out_dir = _artifact_dir(job)
    src_md = ocr_out_md(out_dir, pdf, pages, translate=True)
    src_json = src_md.with_name(src_md.stem + ".pages.json")
    out_md = ocr_out_md(out_dir, pdf, pages, translate=False)
    if not src_json.exists():
        raise RuntimeError(
            f"missing {src_json.name} — did the OCR step fail or get skipped?"
        )
    cmd = [str(config.TRANSLATE_BIN),
           str(src_json), str(out_md), "--to", "English"]
    src_name = _LANG_NAMES.get(params.get("src_lang") or "")
    if src_name and src_name != "English":
        cmd += ["--from", src_name]
    cwd, env = _job_cwd_env(job, out_dir)
    rc = _run_logged(job["id"], cmd, cwd=cwd, env_extra=env)
    if rc != 0:
        raise RuntimeError(f"okforge-translate-pages exited {rc}")


# Probe language codes → the names okforge-translate-pages --from expects.
_LANG_NAMES = {
    "en": "English",
    "es": "Spanish",
    "ca": "Catalan",
    "fr": "French",
    "de": "German",
    "it": "Italian",
}


def _find_chunk_json(kb_dir: Path, pdf: Path, page: int, translate: bool) -> Path:
    """Locate the chunk .pages.json whose page range covers ``page``.

    For translated runs that's the ``_src`` file — the source-language
    truth the re-OCR must splice into."""
    suffix = "_src" if translate else ""
    for stem in dict.fromkeys((_safe_stem(pdf.stem), pdf.stem)):
        pattern = re.compile(
            rf"^{re.escape(stem)}_p(\d+)_(\d+){re.escape(suffix)}\.pages\.json$"
        )
        for pj in sorted((kb_dir / "raw").glob(f"{stem}_p*{suffix}.pages.json")):
            m = pattern.match(pj.name)
            if m and int(m.group(1)) <= page <= int(m.group(2)):
                return pj
        whole = kb_dir / "raw" / f"{stem}{suffix}.pages.json"
        if whole.exists():
            return whole
    raise RuntimeError(
        f"no chunk artifact covering page {page} of {pdf.name} "
        f"(looked for {pdf.stem}_p*{suffix}.pages.json)"
    )


def _splice_page(chunk_json: Path, new_page: dict) -> None:
    """Replace one page's entry in a chunk's .pages.json and regenerate the
    sibling .md (the .md is by contract the join of page contents)."""
    pages = json.loads(chunk_json.read_text(encoding="utf-8"))
    for i, p in enumerate(pages):
        if int(p.get("page", -1)) == int(new_page["page"]):
            pages[i] = new_page
            break
    else:
        pages.append(new_page)
        pages.sort(key=lambda p: int(p.get("page", 0)))
    chunk_json.write_text(
        json.dumps(pages, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    md = chunk_json.with_name(chunk_json.name.replace(".pages.json", ".md"))
    md.write_text(
        "\n\n".join(p["content"] for p in pages if p.get("content")) + "\n",
        encoding="utf-8",
    )


def _run_reocr(job: dict) -> None:
    """Re-OCR ONE page and splice it into its chunk's artifacts instead of
    redoing the whole chunk. For translated runs the page is re-translated
    too, so both the _src pair and the final English pair stay consistent.

    The wiki is NOT regenerated here — re-ingest the chunk afterwards
    (okforge remove + re-add, or Resume on the original full run after a
    remove) to refresh its summary."""
    params = job["params"]
    kb_dir = kbmod.resolve_kb(job["kb"])
    pdf = probe.allowed_pdf_path(params["pdf"])
    pages_spec = _pages_spec(params)
    if not pages_spec or "-" in pages_spec:
        raise ValueError("reocr takes exactly one page, e.g. pages=42")
    page = int(pages_spec)
    translate = bool(params.get("translate"))

    src_json = _find_chunk_json(kb_dir, pdf, page, translate)
    chunk_stem = src_json.name.replace(".pages.json", "")

    tmp = kb_dir / "raw" / f".reocr_job{job['id']}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        # Same out_md stem as the chunk → identical relative image refs
        # (<chunk_stem>_images/pN_imgM.jpg), so spliced content resolves.
        tmp_md = tmp / f"{chunk_stem}.md"
        cmd = _ocr_cmd(pdf, tmp_md, str(page), bool(params.get("figures")),
                       tables=bool(params.get("tables")),
                       prompt_extra=params.get("prompt_extra"))
        rc = _run_logged(job["id"], cmd, cwd=kb_dir,
                         env_extra=_kb_vision_env(kb_dir))
        if rc != 0:
            raise RuntimeError(f"okforge-vision-ocr exited {rc}")
        new_pages = json.loads(
            (tmp / f"{chunk_stem}.pages.json").read_text(encoding="utf-8")
        )
        if not new_pages:
            raise RuntimeError(f"re-OCR produced no content for page {page}")
        new_page = new_pages[0]

        img_tmp = tmp / f"{chunk_stem}_images"
        if img_tmp.is_dir():
            real = kb_dir / "raw" / f"{chunk_stem}_images"
            real.mkdir(exist_ok=True)
            for f in img_tmp.iterdir():
                shutil.move(str(f), real / f.name)

        _splice_page(src_json, new_page)
        _log_line(job["id"], f"spliced page {page} into {src_json.name}")

        if translate:
            single = tmp / "single.pages.json"
            single.write_text(json.dumps([new_page], ensure_ascii=False),
                              encoding="utf-8")
            out_tmp = tmp / "single_out.md"
            cmd = [str(config.TRANSLATE_BIN),
                   str(single), str(out_tmp), "--to", "English"]
            src_name = _LANG_NAMES.get(params.get("src_lang") or "")
            if src_name and src_name != "English":
                cmd += ["--from", src_name]
            rc = _run_logged(job["id"], cmd, cwd=kb_dir,
                             env_extra=_kb_vision_env(kb_dir))
            if rc != 0:
                raise RuntimeError(f"okforge-translate-pages exited {rc}")
            en_page = json.loads(
                (tmp / "single_out.pages.json").read_text(encoding="utf-8")
            )[0]
            final_json = kb_dir / "raw" / f"{chunk_stem.removesuffix('_src')}.pages.json"
            if final_json.exists():
                _splice_page(final_json, en_page)
                _log_line(job["id"], f"spliced page {page} into {final_json.name}")
            else:
                _log_line(job["id"],
                          f"note: {final_json.name} not found — translate step "
                          "hasn't run for this chunk yet, source splice only")
        _log_line(job["id"],
                  "note: wiki not regenerated — re-ingest the chunk "
                  "(okforge remove <doc>, then retry its add) to refresh the summary")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_extract(job: dict) -> None:
    """Extract a text-layer chunk with pymupdf (no LLM) into the same
    .md + .pages.json contract the OCR step produces, so long text-layer
    PDFs can ride the chunk machinery instead of the PageIndex TOC path."""
    import pymupdf

    params = job["params"]
    pdf = probe.allowed_pdf_path(params["pdf"])
    pages_spec = _pages_spec(params)
    out_dir = _artifact_dir(job)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_md = ocr_out_md(out_dir, pdf, pages_spec, translate=False)

    doc = pymupdf.open(pdf)
    try:
        if pages_spec:
            a, _, b = pages_spec.partition("-")
            first, last = int(a), int(b or a)
        else:
            first, last = 1, doc.page_count
        last = min(last, doc.page_count)
        pages = []
        for n in range(first, last + 1):
            text = doc[n - 1].get_text("text").strip()
            if text:
                pages.append({"page": n, "content": text, "images": []})
    finally:
        doc.close()
    if not pages:
        raise RuntimeError(f"no text extracted for pages {first}-{last} of {pdf.name}")
    out_md.write_text(
        "\n\n".join(p["content"] for p in pages) + "\n", encoding="utf-8"
    )
    out_md.with_suffix(".pages.json").write_text(
        json.dumps(pages, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    _log_line(job["id"],
              f"extracted {len(pages)} text page(s) -> {out_md.name} (+ .pages.json)")


def _run_add(job: dict) -> None:
    """`openkb add` one file, skipping if the doc is already indexed —
    that skip is what makes resuming a crashed full run safe."""
    params = job["params"]
    kb_dir = kbmod.resolve_kb(job["kb"])
    path = Path(params["path"])
    if not path.exists():
        raise RuntimeError(
            f"nothing to add: {path.name} does not exist "
            "(did the OCR/translate step fail?)"
        )
    if path.stem in _indexed_stems(kb_dir):
        _log_line(job["id"], f"{path.stem} already indexed in {kb_dir.name}, skipping")
        return
    _snapshot_kb(job["id"], kb_dir, path.stem)
    cmd = [str(config.OPENKB_BIN), "--kb-dir", str(kb_dir), "add", str(path)]
    rc = _run_logged(job["id"], cmd, cwd=kb_dir)
    if rc != 0:
        raise RuntimeError(f"openkb add exited {rc}")
    _okf_lint_after_add(job["id"], kb_dir)
    _reroll_if_empty(job, kb_dir, path, cmd)


def _reroll_if_empty(job: dict, kb_dir: Path, path: Path,
                     add_cmd: list[str]) -> None:
    """Engine nondeterminism guard: the concepts-plan LLM call can
    return a valid-but-EMPTY plan — the add prints [OK], lint is clean,
    but the doc lands with zero concepts/entities (seen live
    2026-07-18 job 607 and 2026-07-19 job 649). When the whole wiki is
    still concept- and entity-free after an add, that's almost
    certainly the bug, not judgment — re-roll ONCE (remove --keep-raw
    + re-add). Bounded at one: a blank document is legitimately
    concept-free, and a near-deterministic model (prefix-cached plan
    prompt, low temperature) can return the same empty plan every
    roll — job 651 did — so looping would only burn LLM calls."""
    wiki = kb_dir / "wiki"
    if kbmod._count(wiki / "concepts") + kbmod._count(wiki / "entities") > 0:
        return
    _log_line(job["id"],
              "[WARN] 0 concepts and 0 entities after add — the engine's "
              "concepts-plan likely returned an empty plan; re-rolling once")
    doc_name = _safe_stem(path.stem)
    proc = subprocess.run(
        [str(config.OPENKB_BIN), "--kb-dir", str(kb_dir),
         "remove", doc_name, "--keep-raw", "--yes"],
        cwd=kb_dir, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=600,
    )
    for line in (proc.stdout + proc.stderr).splitlines():
        _log_line(job["id"], line)
    if proc.returncode != 0:
        _log_line(job["id"],
                  f"remove exited {proc.returncode} — continuing; the re-add "
                  "will skip if the doc is somehow still indexed")
    rc = _run_logged(job["id"], add_cmd, cwd=kb_dir)
    if rc != 0:
        raise RuntimeError(f"openkb add (re-roll) exited {rc}")
    _okf_lint_after_add(job["id"], kb_dir)
    if kbmod._count(wiki / "concepts") + kbmod._count(wiki / "entities") > 0:
        _log_line(job["id"], "re-roll recovered: concepts/entities present now")
    else:
        _log_line(job["id"],
                  "[WARN] still 0 concepts/entities after the re-roll — a "
                  "blank/empty document is legitimately concept-free; "
                  "otherwise use \"Re-ingest chunk\" (stage 4) to roll again")


def _snapshot_kb(job_id: int, kb_dir: Path, label: str) -> None:
    """Auto-commit the KB before an `add`, so a botched ingest is one
    `git reset --hard` away. Initialises the repo on first use (same
    .gitignore convention the existing KB repos follow). Advisory: any
    git failure is logged and never blocks the add."""
    git = ["git", "-C", str(kb_dir)]
    run = lambda args, **kw: subprocess.run(  # noqa: E731
        git + args, capture_output=True, text=True, timeout=180, **kw
    )
    try:
        if not (kb_dir / ".git").is_dir():
            if run(["init", "-q"]).returncode != 0:
                _log_line(job_id, "snapshot: git init failed, skipping")
                return
            gi = kb_dir / ".gitignore"
            if not gi.exists():
                gi.write_text(".env\n__pycache__/\n", encoding="utf-8")
            _log_line(job_id, f"snapshot: initialised git repo in {kb_dir.name}")
        if not run(["status", "--porcelain"]).stdout.strip():
            _log_line(job_id, "snapshot: KB clean, nothing to commit")
            return
        run(["add", "-A"])
        rc = run(["commit", "-q", "-m", f"pre-ingest snapshot before {label}"]).returncode
        if rc == 0:
            _log_line(job_id, f"snapshot: committed KB state before adding {label}")
        else:
            _log_line(job_id, f"snapshot: git commit exited {rc}, continuing")
    except Exception as exc:
        _log_line(job_id, f"snapshot: skipped ({exc})")


def _okf_lint_after_add(job_id: int, kb_dir: Path) -> None:
    """Run the engine's OKF conformance check after an ingest and log the
    result. Advisory only — a conformance issue never fails the add."""
    try:
        proc = subprocess.run(
            [str(config.OPENKB_BIN), "--kb-dir", str(kb_dir), "okf-lint", "--json"],
            cwd=kb_dir,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )
        data = json.loads(proc.stdout)
        issues = data.get("issues", [])
        if issues:
            _log_line(job_id, f"okf-lint: {len(issues)} conformance issue(s):")
            for issue in issues[:20]:
                _log_line(job_id, f"  - {issue}")
        else:
            _log_line(job_id, "okf-lint: clean")
    except Exception as exc:  # advisory: never break an ingest over lint
        _log_line(job_id, f"okf-lint: skipped ({exc})")


def _expand_full(job: dict) -> None:
    """The composite job: split the PDF into chunks and enqueue
    OCR → (translate) → add children per chunk, skipping work whose
    artifacts already exist. Re-queueing the same full job is therefore
    the resume path after a crash or a failed chunk.

    dest="md-out" is the KB-less variant: chunks land in
    MD_OUT_DIR/<out_name>/, no add step, idempotency = file presence."""
    params = job["params"]
    dest = params.get("dest") or "kb"
    if dest == "kb":
        kb_dir = kbmod.resolve_kb(job["kb"])
        out_dir = kb_dir / "raw"
    else:
        out_dir = md_out_dir(params["out_name"])
        out_dir.mkdir(parents=True, exist_ok=True)
    pdf = probe.allowed_pdf_path(params["pdf"])
    chunk_pages = int(params.get("chunk_pages") or config.DEFAULT_CHUNK_PAGES)
    translate = bool(params.get("translate"))
    figures = bool(params.get("figures"))
    # Text-layer mode: pymupdf extraction per chunk instead of OCR — long
    # text PDFs ride the chunk machinery (page-cited short docs), not the
    # engine's PageIndex TOC path.
    text_layer = bool(params.get("text_layer"))

    import pymupdf
    doc = pymupdf.open(pdf)
    page_count = doc.page_count
    doc.close()

    # Optional overall range (skip front matter / back matter): the full
    # job's own pages param, e.g. "7-140". Chunks are laid out from its
    # start page, so resume runs must keep the same range + chunk size
    # for the chunk names to line up.
    first, last = 1, page_count
    range_spec = _pages_spec(params)
    if range_spec:
        a, _, b = range_spec.partition("-")
        first, last = int(a), int(b or a)
        if first > last:
            raise ValueError(f"bad page range: {range_spec}")
        if first > page_count:
            raise ValueError(
                f"start page {first} is beyond the document ({page_count} pages)")
        last = min(last, page_count)

    chunks = []
    s = first
    while s <= last:
        chunks.append((s, min(s + chunk_pages - 1, last)))
        s = chunks[-1][1] + 1

    indexed = _indexed_stems(kb_dir) if dest == "kb" else set()
    _log_line(job["id"],
              f"Planning {pdf.name}: pages {first}-{last} of {page_count} in "
              f"chunks of {chunk_pages}, translate={translate}, figures={figures}"
              + (f", dest={out_dir}" if dest == "md-out" else ""))
    queued_chunks = 0
    for start, end in chunks:
        pages = str(start) if start == end else f"{start}-{end}"
        final_md = ocr_out_md(out_dir, pdf, pages, translate=False)
        src_md = ocr_out_md(out_dir, pdf, pages, translate=True)
        if final_md.stem in indexed:
            _log_line(job["id"],
                      f"chunk {pages}: {final_md.stem} already indexed, skipping")
            continue
        if dest == "md-out" and final_md.exists():
            _log_line(job["id"],
                      f"chunk {pages}: {final_md.name} already on disk, skipping")
            continue
        steps = []
        if not final_md.exists():
            if text_layer:
                steps.append("extract")
            elif translate:
                if not src_md.exists():
                    steps.append("ocr")
                steps.append("translate")
            else:
                steps.append("ocr")
        if dest == "kb":
            steps.append("add")
        base = {"pdf": str(pdf), "pages": pages, "figures": figures,
                "translate": translate, "src_lang": params.get("src_lang"),
                "text_layer": text_layer,
                "endpoint": params.get("endpoint"),
                "chunk_pages": chunk_pages,
                "prompt_extra": params.get("prompt_extra"),
                "dest": dest, "out_name": params.get("out_name")}
        for step in steps:
            child_params = dict(base)
            if step == "add":
                child_params["path"] = str(final_md)
            child = enqueue(step, child_params, kb_name=job["kb"],
                            parent=job["id"])
            _log_line(job["id"], f"chunk {pages}: queued {step} job #{child['id']}")
        queued_chunks += 1
    _log_line(job["id"], f"OK: {queued_chunks} chunk(s) queued, "
                         f"{len(chunks) - queued_chunks} skipped")
    if dest == "md-out" and params.get("auto_ingest"):
        # Top-level (not a child): the serial worker reaches it only
        # after every chunk above finished, and its own add children
        # then group under it in the UI. Idempotent, so a resume of
        # this full job just queues another cheap catch-up ingest.
        child = enqueue("ingest_md",
                        {"src": "md-out", "out_name": params["out_name"],
                         "create_kb": True, "lang": params.get("lang"),
                         "endpoint": params.get("endpoint")},
                        kb_name=params["out_name"])
        _log_line(job["id"], f"queued auto-ingest job #{child['id']} "
                             "(runs after the OCR chunks)")



def _expand_ingest(job: dict) -> None:
    """Decoupled ingest (composite like `full`): make already-produced
    chunk artifacts part of the KB. src="md-out" first copies the run's
    .md/.pages.json/_images from MD_OUT_DIR/<out_name>/ into raw/ —
    skip-if-exists, never overwrite, since a KB-side artifact may have
    been re-OCR'd/spliced since (and a stem colliding across runs keeps
    the first ingest's files). Then one `add` child per not-yet-indexed
    chunk .md; _run_add's indexed-stem skip makes re-running this job
    the resume path.

    create_kb: the decoupled-OCR flow's KB springs into existence at
    first ingest — init it here (worker thread, serial queue, so no
    concurrent-init risk) instead of requiring a separate create call."""
    params = job["params"]
    if params.get("create_kb"):
        try:
            kbmod.resolve_kb(job["kb"])
        except ValueError:
            kbmod.init_kb(job["kb"], params.get("lang") or "en",
                          params.get("endpoint"))
            _log_line(job["id"], f"created KB {job['kb']}")
    kb_dir = kbmod.resolve_kb(job["kb"])
    raw = kb_dir / "raw"
    raw.mkdir(exist_ok=True)
    src = params.get("src") or "kb"
    stems_scope = None
    if src == "md-out":
        src_dir = md_out_dir(params["out_name"])
        if not src_dir.is_dir():
            raise RuntimeError(f"no such md-out run: {src_dir}")
        stems_scope = {p.stem for p in src_dir.glob("*.md")}
        for item in sorted(src_dir.iterdir(), key=lambda p: p.name.lower()):
            if item.name.startswith("."):
                continue
            dest = raw / item.name
            if item.is_file():
                if dest.exists():
                    _log_line(job["id"], f"kept existing {item.name}")
                else:
                    shutil.copy2(item, dest)
                    _log_line(job["id"], f"copied {item.name}")
            elif item.is_dir() and item.name.endswith("_images"):
                dest.mkdir(exist_ok=True)
                copied = 0
                for f in item.iterdir():
                    if not (dest / f.name).exists():
                        shutil.copy2(f, dest / f.name)
                        copied += 1
                _log_line(job["id"],
                          f"copied {copied} new file(s) into {item.name}/")
    indexed = _indexed_stems(kb_dir)
    queued = 0
    for md in sorted(raw.glob("*.md"), key=lambda p: _page_key(p.stem)):
        if md.stem.endswith("_src"):
            continue  # source-language intermediates, never ingested
        if stems_scope is not None and md.stem not in stems_scope:
            continue
        if md.stem in indexed:
            _log_line(job["id"], f"{md.stem} already indexed, skipping")
            continue
        child = enqueue("add", {"path": str(md)},
                        kb_name=job["kb"], parent=job["id"])
        _log_line(job["id"], f"queued add job #{child['id']} for {md.name}")
        queued += 1
    _log_line(job["id"], f"OK: {queued} add job(s) queued")
    # Serial queue: this child runs after every add above has landed.
    # Skipped when nothing new went in AND a description already exists
    # (regenerating on a no-op ingest would waste an LLM call).
    has_desc = bool(str(kbmod.read_config(kb_dir).get("description")
                        or "").strip())
    if queued or not has_desc:
        child = enqueue("describe", {}, kb_name=job["kb"], parent=job["id"])
        _log_line(job["id"],
                  f"queued describe job #{child['id']} (project description)")


def _run_describe(job: dict) -> None:
    """Auto-generate the project description (the MCP 'about' line):
    one LLM call over the index.md document list, run after every
    ingest. Curated descriptions are sacred — the generated text is
    mirrored in <state>/description.auto, and if config.yaml's
    description no longer matches that sidecar a human wrote it and
    this job leaves it alone. Auto ones refresh as the KB grows."""
    kb_dir = kbmod.resolve_kb(job["kb"])
    cfg = kbmod.read_config(kb_dir)
    current = str(cfg.get("description") or "").strip()
    sidecar = config.state_dir(kb_dir) / "description.auto"
    previous_auto = (sidecar.read_text(encoding="utf-8").strip()
                     if sidecar.is_file() else "")
    if current and current != previous_auto:
        _log_line(job["id"], "curated description present — leaving it alone")
        return
    snippets = kbmod.doc_snippets(kb_dir)
    if not snippets:
        _log_line(job["id"], "no documents in index.md yet — nothing to describe")
        return
    base = kbmod._read_env_endpoint(kb_dir)
    model = cfg.get("model")
    if not base or not model:
        _log_line(job["id"], "KB has no endpoint/model configured — skipping")
        return
    doc_list = "\n".join(snippets)[:4000]
    prompt = (
        f"These are the documents in a knowledge base named "
        f"'{kb_dir.name}':\n\n{doc_list}\n\n"
        "Write ONE plain-text sentence (at most 50 words) describing what "
        "this knowledge base covers as a whole. It will be shown to AI "
        "clients choosing which knowledge base to query. No preamble, no "
        "quotes, no markdown — just the sentence."
    )
    # Raw OpenAI protocol like the OCR tools: strip the LiteLLM provider
    # prefix; carry the KB's llm_extra_body (the thinking off-switch).
    payload = {"model": model.split("/", 1)[-1], "max_tokens": 200,
               "messages": [{"role": "user", "content": prompt}]}
    payload.update(cfg.get("llm_extra_body") or {})
    headers = {}
    key = kbmod.read_env_key(kb_dir)
    if key and key != "no-key":
        headers["Authorization"] = f"Bearer {key}"
    _log_line(job["id"],
              f"asking {base} for a description ({len(snippets)} doc(s))...")
    import httpx
    r = httpx.post(base.rstrip("/") + "/chat/completions",
                   headers=headers, json=payload, timeout=180)
    r.raise_for_status()
    text = " ".join(
        r.json()["choices"][0]["message"]["content"].split()).strip(' "\'')
    if not text:
        raise RuntimeError("LLM returned an empty description")
    text = text[:400]
    rc = _run_logged(job["id"],
                     [str(config.OPENKB_BIN), "--kb-dir", str(kb_dir),
                      "describe", text],
                     cwd=kb_dir)
    if rc != 0:
        raise RuntimeError(f"okforge describe exited {rc}")
    sidecar.write_text(text + "\n", encoding="utf-8")
    _log_line(job["id"], f"description set: {text}")


def _run_recompile(job: dict) -> None:
    """Re-ingest the chunk containing the given page: engine remove with
    --keep-raw --yes (artifacts stay), then a fresh add of the chunk .md.

    The first-class replacement for the manual reocr → CLI remove →
    resume dance (which hit two live traps on 2026-07-07). Run it after
    one or more table-mode re-OCRs have been spliced into the chunk."""
    params = job["params"]
    kb_dir = kbmod.resolve_kb(job["kb"])
    pdf = probe.allowed_pdf_path(params["pdf"])
    pages_spec = _pages_spec(params)
    if not pages_spec or "-" in pages_spec:
        raise ValueError("recompile takes the page number whose chunk to re-ingest")
    page = int(pages_spec)

    chunk_json = _find_chunk_json(kb_dir, pdf, page, translate=False)
    chunk_md = chunk_json.with_name(chunk_json.name.replace(".pages.json", ".md"))
    if not chunk_md.exists():
        raise RuntimeError(f"chunk file missing: {chunk_md.name}")
    doc_name = _safe_stem(chunk_md.stem)

    _log_line(job["id"], f"recompiling chunk {chunk_md.stem} (doc {doc_name})")
    proc = subprocess.run(
        [str(config.OPENKB_BIN), "--kb-dir", str(kb_dir),
         "remove", doc_name, "--keep-raw", "--yes"],
        cwd=kb_dir, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=600,
    )
    for line in (proc.stdout + proc.stderr).splitlines():
        _log_line(job["id"], line)
    if proc.returncode != 0:
        _log_line(job["id"],
                  f"remove exited {proc.returncode} — continuing; the add "
                  "will skip if the doc is somehow still indexed")

    _run_add({"id": job["id"], "kb": job["kb"], "params": {"path": str(chunk_md)}})


def _run_publish(job: dict) -> None:
    """Build the KB's wiki into a static Quartz site at SITES_DIR/<name>.

    Writes the per-KB quartz.config.yaml (pageTitle from the KB's
    optional ``site_title`` config key, else its name; baseUrl =
    PUBLIC_SITE_HOST/<name>) and runs the shared Quartz install. Making
    the site public stays a manual rsync of the output dir — see
    KB-OPERATIONS.md."""
    import yaml

    kb_dir = kbmod.resolve_kb(job["kb"])
    wiki = kb_dir / "wiki"
    if not (wiki / "index.md").exists():
        raise RuntimeError(f"{kb_dir.name} has no wiki to publish")
    default_cfg = config.QUARTZ_DIR / "quartz.config.default.yaml"
    if not default_cfg.exists():
        raise RuntimeError(f"quartz install not found at {config.QUARTZ_DIR}")

    kb_cfg = {}
    cfg_path = config.state_dir(kb_dir) / "config.yaml"
    if cfg_path.is_file():
        kb_cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    # Title preference: explicit site_title > curated description (when it
    # reads like a title) > the KB's directory name.
    description = str(kb_cfg.get("description") or "").strip()
    site_title = str(
        kb_cfg.get("site_title")
        or (description if 0 < len(description) <= 60 else "")
        or kb_dir.name
    )
    site_suffix = str(kb_cfg.get("site_title_suffix") or "")

    qc = yaml.safe_load(default_cfg.read_text(encoding="utf-8")) or {}
    conf = qc.setdefault("configuration", {})
    conf["pageTitle"] = site_title
    conf["pageTitleSuffix"] = site_suffix
    conf["baseUrl"] = f"{config.PUBLIC_SITE_HOST}/{kb_dir.name}"
    ignore = conf.setdefault("ignorePatterns", [])
    for pat in ("AGENTS.md", "log.md"):
        if pat not in ignore:
            ignore.append(pat)
    # The default config is a live file with one KB's leftovers (footer
    # links etc.) — reset per-KB surfaces so nothing leaks between sites.
    for plugin in qc.get("plugins") or []:
        if isinstance(plugin, dict) and str(plugin.get("source", "")).endswith("/footer"):
            plugin["options"] = {"links": {site_title: ""}}
    (config.QUARTZ_DIR / "quartz.config.yaml").write_text(
        yaml.safe_dump(qc, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    _log_line(job["id"],
              f"quartz config: title={site_title!r} baseUrl={conf['baseUrl']}")

    out = config.SITES_DIR / kb_dir.name
    out.mkdir(parents=True, exist_ok=True)
    cmd = [str(config.NODE_BIN), "quartz/bootstrap-cli.mjs", "build",
           "--directory", str(wiki), "--output", str(out)]
    rc = _run_logged(job["id"], cmd, cwd=config.QUARTZ_DIR)
    if rc != 0:
        raise RuntimeError(f"quartz build exited {rc}")
    _log_line(job["id"], f"site built: {out}")
    _log_line(job["id"],
              "to publish publicly: rsync -av --delete "
              f"{out}/ <user>@{config.PUBLIC_SITE_HOST}:<docroot>/{kb_dir.name}/")


_RUNNERS = {
    "pilot": _run_pilot,
    "ocr": _run_ocr,
    "translate": _run_translate,
    "add": _run_add,
    "full": _expand_full,
    "reocr": _run_reocr,
    "extract": _run_extract,
    "publish": _run_publish,
    "recompile": _run_recompile,
    "ingest_md": _expand_ingest,
    "describe": _run_describe,
}


# --------------------------------------------------------------- worker

def _next_queued() -> dict | None:
    with _db_lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
    return _row_to_dict(row) if row else None


def _worker_loop() -> None:
    while True:
        job = _next_queued()
        if job is None:
            time.sleep(1)
            continue
        job_id = job["id"]
        with _current_lock:
            _current["job_id"] = job_id
        _set_status(job_id, "running")
        try:
            runner = _RUNNERS.get(job["type"])
            if runner is None:
                raise ValueError(f"no runner for job type {job['type']!r}")
            runner(job)
        except Exception as e:
            # cancel() may have marked it already; don't overwrite that
            if get_job(job_id)["status"] == "running":
                _set_status(job_id, "failed", error=str(e))
            with log_path(job_id).open("a", encoding="utf-8") as log:
                log.write(f"ERROR: {e}\n")
        else:
            if get_job(job_id)["status"] == "running":
                _set_status(job_id, "done")
        finally:
            with _current_lock:
                _current["job_id"] = None


# Job logs older than this are pruned at startup (their jobs are long
# terminal; the sqlite rows — tiny — are kept as history).
LOG_RETENTION_DAYS = 30


def _rotate_logs() -> None:
    cutoff = time.time() - LOG_RETENTION_DAYS * 86400
    active = {
        j["id"] for j in list_jobs(limit=500) if j["status"] not in TERMINAL
    }
    for lp in config.JOB_LOG_DIR.glob("job_*.log"):
        try:
            job_id = int(lp.stem.split("_")[1])
        except (IndexError, ValueError):
            continue
        try:
            if job_id not in active and lp.stat().st_mtime < cutoff:
                lp.unlink()
        except OSError:
            pass


# Terminal job rows older than this are pruned at startup; the newest
# HISTORY_KEEP_MIN rows always survive (ETA stats read the last 40 per
# type; retry/resume re-read params from recent rows).
HISTORY_RETENTION_DAYS = 90
HISTORY_KEEP_MIN = 200


def _prune_history() -> None:
    cutoff = time.time() - HISTORY_RETENTION_DAYS * 86400
    with _db_lock, _conn() as c:
        row = c.execute(
            "SELECT min(id) FROM (SELECT id FROM jobs ORDER BY id DESC LIMIT ?)",
            (HISTORY_KEEP_MIN,),
        ).fetchone()
        min_keep = row[0] or 0
        c.execute(
            "DELETE FROM jobs WHERE status IN ('done','failed','cancelled') "
            "AND finished IS NOT NULL AND finished < ? AND id < ?",
            (cutoff, min_keep),
        )


def start_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    _init_db()
    _rotate_logs()
    _prune_history()
    # Orphaned 'running' jobs = backend died mid-job. Their subprocesses
    # run in their own sessions and survive our death, so kill any
    # leftovers first — otherwise the re-queued job would run OCR/add
    # concurrently with its own ghost, the exact thing the serial queue
    # exists to prevent.
    with _db_lock, _conn() as c:
        orphans = c.execute(
            "SELECT id, pid FROM jobs WHERE status = 'running'"
        ).fetchall()
    for row in orphans:
        if row["pid"]:
            _terminate_tree(row["pid"])
    with _db_lock, _conn() as c:
        c.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'")
    threading.Thread(target=_worker_loop, name="job-worker", daemon=True).start()
