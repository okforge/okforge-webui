"""MCP server for okforge, mounted at /mcp on the web UI backend.

Exposes the KBs to any MCP client over streamable HTTP (Claude Code,
Claude Desktop, etc.):

    claude mcp add --transport http okforge http://okforge.local/mcp

Three tools, mirroring the "pick a project / check status / ask" flow:
list_projects, project_status, ask. Read-only by design — ingest stays in
the web UI's job queue and the CLI. Stateless + JSON responses so plain
POSTs work and nothing needs SSE through Apache.
"""

import asyncio
import json
import os
import re
from pathlib import Path

import yaml
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import config, jobs, kb

mcp = FastMCP(
    "okforge",
    instructions=(
        "Query Dana's local okforge knowledge bases (one per subject, built "
        "from scanned books via OCR). Workflow: call list_projects once, "
        "pick the single project whose 'about' text best matches the "
        "question, then search(project, query) for fact lookups (fast, no "
        "LLM) + read_wiki_page for full pages; use ask(project, question) "
        "only when you need synthesis — each ask runs a full retrieval-and-"
        "generation pass on a local LLM and takes 1-3 minutes, so NEVER "
        "send the same question to several projects. If no project is an "
        "obvious match, ask the user which one they mean instead of "
        "guessing. Answers cite source pages as (p. N)."
    ),
    # mounted as a sub-app at /mcp, so serve at the mount root
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    # The SDK's DNS-rebinding protection only allows localhost Hosts and
    # 421s anything arriving through the Apache proxy (Host: openkb.local).
    # This service is LAN/VPN-only with no auth (PLAN.md), so the browser
    # attack surface the protection targets isn't meaningful here.
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# `openkb query` runs tree search + answer generation; a long one on the
# busy single-slot host can take a few minutes.
QUERY_TIMEOUT = 600


def _about(kb_dir: Path) -> str | None:
    """Topical one-liner for project picking.

    Prefers the curated project-level description in .okforge/config.yaml
    (set via `okforge describe`) — the doc-summary fallback misleads once a
    project holds many books."""
    cfg_path = config.state_dir(kb_dir) / "config.yaml"
    if cfg_path.is_file():
        try:
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            curated = str(cfg.get("description") or "").strip()
            if curated:
                return curated[:400]
        except yaml.YAMLError:
            pass
    idx = kb_dir / "wiki" / "index.md"
    if not idx.is_file():
        return None
    snippets = []
    in_docs = False
    for line in idx.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_docs = line.startswith("## Documents")
            continue
        if in_docs and line.lstrip().startswith("- ") and "—" in line:
            snippets.append(line.split("—", 1)[1].strip())
            if len(snippets) >= 4:
                break
    return " ".join(snippets)[:400] or None


@mcp.tool()
def list_projects() -> list[dict]:
    """List all knowledge-base projects: content stats plus an 'about'
    line describing what each project covers. Use the 'about' text to
    pick the ONE project relevant to the user's question before calling
    ask — asking multiple projects wastes minutes per call."""
    out = []
    for info in kb.list_kbs():
        info["about"] = _about(Path(info["path"]))
        out.append(info)
    return out


@mcp.tool()
def project_status(project: str) -> dict:
    """Status of one project: content stats, LLM endpoint, and the state
    of the ingest job queue (anything running or queued for this KB)."""
    kb_dir = kb.resolve_kb(project)  # raises ValueError on unknown name
    info = kb.kb_info(kb_dir)
    recent = jobs.list_jobs(limit=50)
    mine = [j for j in recent if j["kb"] == project]
    info["jobs"] = {
        "running": [
            {"id": j["id"], "type": j["type"], "pages": j["params"].get("pages")}
            for j in mine if j["status"] == "running"
        ],
        "queued": sum(1 for j in mine if j["status"] == "queued"),
        "failed_recently": [
            {"id": j["id"], "type": j["type"], "error": j["error"]}
            for j in mine[:10] if j["status"] == "failed"
        ],
    }
    return info


# Wiki dirs search() scans for .md matches (index.md is scanned too).
_SEARCH_DIRS = ("summaries", "concepts", "entities", "explorations")
_SNIPPET_LEN = 220
_MAX_FILE_BYTES = 2_000_000  # skip pathological files


def _snippet(line: str, needle_pos: int) -> str:
    """Trim a matching line to ~220 chars centred on the match."""
    line = line.strip()
    if len(line) <= _SNIPPET_LEN:
        return line
    start = max(0, needle_pos - _SNIPPET_LEN // 2)
    return ("…" if start else "") + line[start : start + _SNIPPET_LEN] + "…"


@mcp.tool()
def search(project: str, query: str, max_results: int = 20) -> list[dict]:
    """Fast lexical search over ONE project's wiki — no LLM, sub-second.
    Use this FIRST for fact lookups (names, dates, places, part numbers);
    only fall back to ask() when you need synthesis across sources.
    Case-insensitive substring match. Returns wiki-relative path, a
    snippet per hit, and the real source page number when the hit is in
    a document's per-page text (cite as "p. N"). Pair with
    read_wiki_page(project, path) to pull a whole page."""
    kb_dir = kb.resolve_kb(project)
    wiki = kb_dir / "wiki"
    needle = query.strip().lower()
    if not needle:
        raise ValueError("empty query")
    results: list[dict] = []

    def _scan_md(path: Path) -> None:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return
        rel = str(path.relative_to(wiki))
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            pos = line.lower().find(needle)
            if pos != -1:
                results.append({"path": rel, "snippet": _snippet(line, pos)})
                if len(results) >= max_results:
                    return

    # Wiki pages (generated content) first — most concentrated signal.
    for sub in _SEARCH_DIRS:
        d = wiki / sub
        if not d.is_dir():
            continue
        for md in sorted(d.rglob("*.md")):
            _scan_md(md)
            if len(results) >= max_results:
                return results
    if (wiki / "index.md").is_file():
        _scan_md(wiki / "index.md")

    # Per-page source text: hits here carry a REAL page number.
    sources = wiki / "sources"
    if sources.is_dir():
        for pj in sorted(sources.glob("*.json")):
            if len(results) >= max_results:
                break
            if pj.stat().st_size > _MAX_FILE_BYTES * 5:
                continue
            try:
                pages = json.loads(pj.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(pages, list):
                continue
            for page in pages:
                if not isinstance(page, dict):
                    continue
                content = str(page.get("content", ""))
                pos = content.lower().find(needle)
                if pos == -1:
                    continue
                line = next(
                    (ln for ln in content.splitlines() if needle in ln.lower()), content[:200]
                )
                results.append(
                    {
                        "path": str(pj.relative_to(wiki)),
                        "page": page.get("page"),
                        "snippet": _snippet(line, max(0, line.lower().find(needle))),
                    }
                )
                if len(results) >= max_results:
                    break
    return results


@mcp.tool()
def read_wiki_page(project: str, path: str) -> str:
    """Read one wiki page from a project by its wiki-relative path (as
    returned by search), e.g. 'summaries/doc.md' or 'entities/fort-marion.md'.
    Markdown pages return their full text; a 'sources/<doc>.json' path
    returns that document's per-page text (page numbers preserved).
    Cheap and instant — prefer search + this over ask() for lookups."""
    kb_dir = kb.resolve_kb(project)
    wiki = kb_dir / "wiki"
    target = (wiki / path).resolve()
    if not target.is_relative_to(wiki.resolve()):
        raise ValueError(f"path escapes the wiki: {path!r}")
    if target.suffix not in (".md", ".json") or not target.is_file():
        raise ValueError(f"no such wiki page: {path!r}")
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > 200_000:
        text = text[:200_000] + "\n…(truncated)"
    return text


@mcp.tool()
async def ask(project: str, question: str, ctx: Context | None = None) -> str:
    """Ask ONE project's knowledge base a question; returns the answer
    with (p. N) page citations. Expensive: a full retrieval + generation
    pass on a local LLM, typically 1-3 minutes — for simple fact lookups
    use search + read_wiki_page instead. Pick the right project first via
    list_projects' 'about' text — do not fan the same question out across
    projects."""
    kb_dir = kb.resolve_kb(project)
    question = question.strip()
    if not question:
        raise ValueError("empty question")
    env = os.environ.copy()
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

    # Heartbeat progress so clients that honor progress tokens keep the
    # call alive across the multi-minute query. Best-effort: in this
    # server's stateless-JSON mode most clients never see interim
    # notifications, so failures are swallowed.
    async def _communicate():
        elapsed = 0
        comm = asyncio.create_task(proc.communicate())
        while True:
            done, _ = await asyncio.wait({comm}, timeout=15)
            if done:
                return comm.result()
            elapsed += 15
            if ctx is not None:
                try:
                    await ctx.report_progress(
                        min(elapsed, QUERY_TIMEOUT), QUERY_TIMEOUT,
                        f"query running ({elapsed}s elapsed)",
                    )
                except Exception:
                    pass

    try:
        out, _ = await asyncio.wait_for(_communicate(), timeout=QUERY_TIMEOUT)
    except TimeoutError:
        proc.terminate()
        raise RuntimeError(f"openkb query timed out after {QUERY_TIMEOUT}s")
    text = _ANSI_RE.sub("", out.decode("utf-8", errors="replace")).strip()
    if proc.returncode != 0:
        raise RuntimeError(f"openkb query exited {proc.returncode}: {text[-500:]}")
    return text
