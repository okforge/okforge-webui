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
from pathlib import Path, PurePosixPath

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import config, jobs, kb

mcp = FastMCP(
    "okforge",
    # This block is the ONE place the search-vs-ask routing rule is
    # stated. Repeating it in the tool docstrings gave weak client models
    # four versions of the same judgment call to re-litigate, and some
    # ruminated past their thinking budget without ever calling anything.
    # Keep the rule here, deterministic; keep docstrings descriptive.
    instructions=(
        "Query local okforge knowledge bases — one per subject, each a "
        "citation-backed wiki compiled from source documents (books, "
        "papers, video transcripts).\n\n"
        "Start by calling list_projects and picking the ONE project whose "
        "'about' text matches the question. If none is an obvious match, "
        "ask the user which they mean instead of guessing. Query that one "
        "project; to compare knowledge bases, query them one at a time.\n\n"
        "Choosing a tool: default to search, then read_wiki_page on the "
        "hits worth citing. Use ask only when the question calls for a "
        "summary, comparison, or explanation spanning multiple documents.\n\n"
        "Wiki pages cite their source pages as (p. N) where the documents "
        "were ingested page by page. Carry those citations into your own "
        "answer, next to the claim each one supports — tracing a statement "
        "back to its source page is the point of these knowledge bases, and "
        "an uncited answer throws that away. In video-transcript knowledge "
        "bases page N is the N-th 5-minute block of the video, so give the "
        "timestamp too: (p. 14) = minutes 65-70.\n\n"
        "read_wiki_page(project, 'AGENTS.md') returns a KB's schema "
        "documentation; the MCP prompt 'kb-search-guide' has the full "
        "recommended search strategy."
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

# Canonical client guidance lives in docs/MCP_CLIENT_PROMPT.md (the
# fenced block); served here as an MCP prompt so capable clients can
# pull it instead of pasting it by hand.
_CLIENT_PROMPT_DOC = config.REPO_DIR / "docs" / "MCP_CLIENT_PROMPT.md"


def _client_prompt_text() -> str:
    """The recommended client prompt: the doc's marked fenced block.

    The doc carries a second, condensed block for small models behind
    clients that drop the server instructions. Only a client that can
    fetch MCP prompts at all gets this one, and those are the capable
    ones, so it serves the full text — selected by marker rather than
    by position, so reordering the doc can't silently swap them.
    """
    doc = _CLIENT_PROMPT_DOC.read_text(encoding="utf-8")
    m = re.search(
        r"<!-- kb-search-guide -->\s*```text\n(.*?)\n```", doc, flags=re.DOTALL
    )
    if not m:
        raise RuntimeError(
            f"no <!-- kb-search-guide --> ```text block in {_CLIENT_PROMPT_DOC}"
        )
    return m.group(1)


@mcp.prompt(
    name="kb-search-guide",
    title="How to search these knowledge bases",
    description=(
        "Recommended strategy for querying the okforge KBs through this "
        "server's tools: project selection, cheap-path-first search, wiki "
        "layout, when ask() is justified, and citation conventions. Inject "
        "as system-level guidance for any model using these tools."
    ),
)
def kb_search_guide() -> str:
    return _client_prompt_text()

# `openkb query` runs tree search + answer generation; a long one on the
# busy single-slot host can take a few minutes.
QUERY_TIMEOUT = 600


def _about(kb_dir: Path) -> str | None:
    """Topical one-liner for project picking.

    Prefers the curated project-level description in .okforge/config.yaml
    (set via `okforge describe`) — the doc-summary fallback misleads once a
    project holds many books."""
    curated = str(kb.read_config(kb_dir).get("description") or "").strip()
    if curated:
        return curated[:400]
    snippets = [s.split("—", 1)[1].strip()
                for s in kb.doc_snippets(kb_dir, limit=8) if "—" in s][:4]
    return " ".join(snippets)[:400] or None


# Client-facing slice of a KB record. The full kb.list_kbs() row also
# carries server internals — filesystem paths, endpoint URLs, the publish
# rsync destination — that no MCP client needs and that double the payload.
_LIST_FIELDS = ("name", "language", "docs", "concepts", "entities",
                "images", "citations")


@mcp.tool(structured_output=False)
def list_projects() -> list[dict]:
    """List all knowledge-base projects: content stats plus an 'about'
    line describing what each project covers. Use the 'about' text to
    pick the one project relevant to the user's question."""
    out = []
    for info in kb.list_kbs():
        row = {"name": info["name"], "about": _about(Path(info["path"]))}
        row.update({k: info.get(k) for k in _LIST_FIELDS if k != "name"})
        out.append(row)
    return out


@mcp.tool(structured_output=False)
def project_status(project: str) -> dict:
    """Status of one project: content stats, LLM endpoint, and the state
    of the ingest job queue (anything running or queued for this KB)."""
    kb_dir = kb.resolve_kb(project)  # raises ValueError on unknown name
    info = kb.kb_info(kb_dir)
    # Active jobs via the dedicated filter: a big run floods the
    # newest-N window with its own children, so a windowed fetch would
    # miss this (or any other) KB's queued/running rows entirely.
    mine_active = [
        j for j in jobs.list_jobs(limit=1000, active=True)
        if j["kb"] == project
    ]
    mine_recent = [
        j for j in jobs.list_jobs(limit=1000) if j["kb"] == project
    ][:10]
    info["jobs"] = {
        "running": [
            {"id": j["id"], "type": j["type"], "pages": j["params"].get("pages")}
            for j in mine_active if j["status"] == "running"
        ],
        "queued": sum(1 for j in mine_active if j["status"] == "queued"),
        "failed_recently": [
            {"id": j["id"], "type": j["type"], "error": j["error"]}
            for j in mine_recent if j["status"] == "failed"
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


@mcp.tool(structured_output=False)
def search(project: str, query: str, max_results: int = 20) -> list[dict]:
    """Lexical search over ONE project's wiki: case-insensitive substring
    match, sub-second, no LLM. Finds names, dates, places, part numbers
    and the passages around them. Returns a wiki-relative path and a
    snippet per hit, plus the source page number when the hit is in a
    document's per-page text (cite as "p. N"). Read a whole hit with
    read_wiki_page(project, path)."""
    kb_dir = kb.resolve_kb(project)
    wiki = kb_dir / "wiki"
    needle = query.strip().lower()
    if not needle:
        raise ValueError("empty query")
    results: list[dict] = []

    def _scan_md(path: Path) -> None:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return
        rel = path.relative_to(wiki).as_posix()
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
                        "path": pj.relative_to(wiki).as_posix(),
                        "page": page.get("page"),
                        "snippet": _snippet(line, max(0, line.lower().find(needle))),
                    }
                )
                if len(results) >= max_results:
                    break
    return results


def _resolve_wiki_page(wiki: Path, path: str) -> Path:
    """Locate a wiki page, tolerating the links the KB itself publishes.

    index.md and every summary's "Related Concepts" block emit flat
    wikilinks like `concepts/simulation-hypothesis` (no extension), but
    in a large KB the page is nested under topic folders — 812 of
    ScottAdamsCoffee2025's 814 concept pages are. Taking those links
    literally is the obvious client move and it used to dead-end, so
    fall back to matching the basename under the named top-level dir.
    """
    # Tolerate Windows-style separators from clients that cached them.
    rel = path.replace("\\", "/").strip("/")
    target = (wiki / rel).resolve()
    if not target.is_relative_to(wiki.resolve()):
        raise ValueError(f"path escapes the wiki: {path!r}")
    if target.is_file() and target.suffix in (".md", ".json"):
        return target

    stem = PurePosixPath(rel).name
    if not stem:
        raise ValueError(f"no such wiki page: {path!r}")
    wanted = ({stem} if stem.endswith((".md", ".json"))
              else {f"{stem}.md", f"{stem}.json"})
    # A leading real directory scopes the search, which both speeds it up
    # and keeps same-named pages in different sections from colliding.
    head = PurePosixPath(rel).parts[0]
    root = wiki / head if len(PurePosixPath(rel).parts) > 1 and (wiki / head).is_dir() else wiki
    hits = sorted({p for name in wanted for p in root.rglob(name) if p.is_file()})

    if not hits:
        raise ValueError(f"no such wiki page: {path!r}")
    if len(hits) > 1:
        # Never guess between distinct pages — the caller cites what it
        # reads, so silently picking one would mis-source the answer.
        opts = ", ".join(p.relative_to(wiki).as_posix() for p in hits[:8])
        raise ValueError(
            f"{path!r} is ambiguous — {len(hits)} pages share that name. "
            f"Retry with a full path: {opts}"
        )
    return hits[0]


@mcp.tool(structured_output=False)
def read_wiki_page(project: str, path: str) -> str:
    """Read one wiki page from a project by its wiki-relative path (as
    returned by search), e.g. 'summaries/doc.md' or 'entities/fort-marion.md'.
    Markdown pages return their full text; a 'sources/<doc>.json' path
    returns that document's per-page text (page numbers preserved).
    A flat wikilink such as 'concepts/simulation-hypothesis' also
    resolves, as long as only one page in that section has the name.
    The text carries (p. N) source-page citations — keep them in your
    answer, next to the claim each one supports."""
    kb_dir = kb.resolve_kb(project)
    wiki = kb_dir / "wiki"
    target = _resolve_wiki_page(wiki, path)
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > 200_000:
        text = text[:200_000] + "\n…(truncated)"
    return text


@mcp.tool(structured_output=False)
async def ask(project: str, question: str, ctx: Context | None = None) -> str:
    """Ask ONE project's knowledge base a question; returns a written
    answer citing source pages as (p. N) where that project's documents
    were ingested page by page — keep those citations in your reply.
    Runs a full retrieval and generation pass on a local LLM, typically
    1-3 minutes."""
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
