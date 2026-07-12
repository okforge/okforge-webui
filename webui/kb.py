"""KB discovery and creation.

A KB is any directory directly under KB_ROOT containing a state dir —
.okforge/, or a not-yet-migrated KB's legacy .openkb/ (see
config.state_dir()) — (AGENTS.md's ~/<Subject>/ convention). Creation
wraps `openkb init`
non-interactively — with -m/-l given and stdin piped, the only remaining
prompt is the API-key one, satisfied by a blank line (PLAN.md's
"printf '\\n' |" risk item) — then writes the .env the README prescribes.
"""

import re
import shutil
import subprocess
import time
from pathlib import Path

import yaml

from . import config

KB_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def kb_dirs() -> list[Path]:
    return sorted(
        (
            d
            for d in config.KB_ROOT.iterdir()
            if d.is_dir() and config.state_dir(d).is_dir()
        ),
        key=lambda d: d.name.lower(),
    )


def resolve_kb(name: str) -> Path:
    """Map a client-supplied KB name to its directory. Raises ValueError."""
    if not KB_NAME_RE.match(name):
        raise ValueError(f"invalid KB name: {name!r}")
    d = config.KB_ROOT / name
    if not config.state_dir(d).is_dir():
        raise ValueError(f"no such KB: {name}")
    return d


def _read_env_endpoint(kb_dir: Path) -> str | None:
    env = kb_dir / ".env"
    if not env.is_file():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("OPENAI_API_BASE="):
            return line.split("=", 1)[1].strip()
    return None


def endpoint_label(kb_dir: Path) -> str | None:
    """The configured endpoint this KB points at, from its .env's
    OPENAI_API_BASE — None if the URL matches no current endpoint."""
    url = _read_env_endpoint(kb_dir)
    return next(
        (label for label, u in config.ENDPOINTS.items() if u == url), None
    )


def _count(pattern_dir: Path, glob: str = "*.md") -> int:
    # rglob: topic-tree KBs nest concepts under concepts/<topic>/;
    # _topic.md files are tree nodes, not concepts.
    if not pattern_dir.is_dir():
        return 0
    return sum(1 for p in pattern_dir.rglob(glob) if p.name != "_topic.md")


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _count_images(images_dir: Path) -> int:
    # Images land in per-document subdirectories (e.g.
    # sources/images/<doc>/p41_img3.jpg), so count recursively.
    if not images_dir.is_dir():
        return 0
    return sum(
        1
        for p in images_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )


_CITATION_RE = re.compile(r"\(pp?\.\s*\d")


def _count_citations(wiki_dir: Path) -> int:
    """Page citations like (p. 5) / (pp. 3-4) across the wiki markdown —
    the number patch 5 exists to make nonzero."""
    total = 0
    for sub in ("summaries", "concepts", "entities"):
        d = wiki_dir / sub
        if not d.is_dir():
            continue
        for f in d.rglob("*.md"):
            total += len(_CITATION_RE.findall(f.read_text(encoding="utf-8")))
    return total


def kb_info(kb_dir: Path) -> dict:
    cfg = {}
    cfg_path = config.state_dir(kb_dir) / "config.yaml"
    if cfg_path.is_file():
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    endpoint_url = _read_env_endpoint(kb_dir)
    endpoint = endpoint_label(kb_dir)
    wiki = kb_dir / "wiki"
    return {
        "name": kb_dir.name,
        "path": str(kb_dir),
        "model": cfg.get("model"),
        "language": cfg.get("language"),
        "endpoint": endpoint,
        "endpoint_url": endpoint_url,
        "docs": _count(wiki / "summaries"),
        "concepts": _count(wiki / "concepts"),
        "entities": _count(wiki / "entities"),
        "images": _count_images(wiki / "sources" / "images"),
        "citations": _count_citations(wiki),
        # Quartz site built for this KB (webui shows a "view site" link)
        "published": (config.SITES_DIR / kb_dir.name / "index.html").is_file(),
        "publish_cmd": (
            f"rsync -av --delete {config.SITES_DIR / kb_dir.name}/ "
            f"{config.PUBLIC_SITE_DEST}/{kb_dir.name}/"
        ),
        "raw_files": _count(kb_dir / "raw", "*.*"),
    }


def list_kbs() -> list[dict]:
    return [kb_info(d) for d in kb_dirs()]


def init_kb(name: str, lang: str = "en", endpoint: str | None = None) -> dict:
    if not KB_NAME_RE.match(name):
        raise ValueError(f"invalid KB name: {name!r}")
    if not re.match(r"^[a-z]{2}(-[A-Za-z]{2,4})?$", lang):
        raise ValueError(f"invalid language code: {lang!r}")
    endpoint = endpoint or config.DEFAULT_ENDPOINT
    if endpoint not in config.ENDPOINTS:
        raise ValueError(f"unknown endpoint: {endpoint!r}")

    kb_dir = config.KB_ROOT / name
    if config.state_dir(kb_dir).is_dir():
        raise FileExistsError(f"KB already exists: {name}")
    kb_dir.mkdir(exist_ok=True)

    # `openkb init` unconditionally makes the new KB the global default
    # (register_kb in openkb/config.py). The web UI always passes
    # --kb-dir, so silently stealing the user's CLI default would be
    # pure downside — remember it and put it back afterwards.
    prev_default = _read_default_kb()

    model = config.endpoint_model(endpoint)
    # --json: okforge's non-interactive machine mode (no prompts at all,
    # so no stdin newline for the old API-key prompt is needed).
    proc = subprocess.run(
        [str(config.OPENKB_BIN), "init", "-m", model, "-l", lang, "--json"],
        cwd=kb_dir,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0 or not config.state_dir(kb_dir).is_dir():
        raise RuntimeError(
            f"openkb init failed (rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )

    env_path = kb_dir / ".env"
    if not env_path.exists():
        # OPENAI_API_BASE doubles as the KB→endpoint-label mapping read
        # back by _read_env_endpoint, so it's written even for providers
        # (openrouter/...) whose LiteLLM route ignores it.
        env_path.write_text(
            f"LLM_API_KEY={config.endpoint_key(endpoint)}\n"
            f"OPENAI_API_BASE={config.ENDPOINTS[endpoint]}\n",
            encoding="utf-8",
        )

    # MANDATORY: Qwen3 serves with thinking ON by default; without this
    # block every add pays a hidden reasoning pass (measured 27 min ->
    # 2.3 min per 20-page chunk on llama.cpp). Each API dialect spells
    # "don't think" differently: chat_template_kwargs for llama.cpp/vLLM,
    # reasoning.enabled for OpenRouter. KB-OPERATIONS.md.
    if model.startswith("openrouter/"):
        thinking_off = (
            "llm_extra_body:\n"
            "  reasoning:\n"
            "    enabled: false\n"
        )
    else:
        thinking_off = (
            "llm_extra_body:\n"
            "  chat_template_kwargs:\n"
            "    enable_thinking: false\n"
        )
    cfg_path = config.state_dir(kb_dir) / "config.yaml"
    if cfg_path.is_file() and "llm_extra_body" not in cfg_path.read_text(encoding="utf-8"):
        with cfg_path.open("a", encoding="utf-8") as f:
            f.write(thinking_off)

    if prev_default and Path(prev_default).is_dir():
        subprocess.run(
            [str(config.OPENKB_BIN), "use", prev_default],
            capture_output=True,
            timeout=30,
        )
    return kb_info(kb_dir)


def _read_default_kb() -> str | None:
    gc_path = Path.home() / ".config" / "okforge" / "global.yaml"
    if not gc_path.is_file():
        gc_path = Path.home() / ".config" / "openkb" / "global.yaml"
        if not gc_path.is_file():
            return None
    try:
        gc = yaml.safe_load(gc_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    return gc.get("default_kb")


def _scrub_global_registry(kb_path: str) -> None:
    """Drop a retired KB from the engine's global config: its known_kbs
    entry, and default_kb when it points there — a dangling default
    would send the next bare `openkb add` into a moved directory."""
    for gc_path in (Path.home() / ".config" / "okforge" / "global.yaml",
                    Path.home() / ".config" / "openkb" / "global.yaml"):
        if not gc_path.is_file():
            continue
        try:
            gc = yaml.safe_load(gc_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        changed = False
        if gc.get("default_kb") == kb_path:
            gc.pop("default_kb")
            changed = True
        known = gc.get("known_kbs")
        if isinstance(known, list) and kb_path in known:
            gc["known_kbs"] = [p for p in known if p != kb_path]
            changed = True
        if changed:
            gc_path.write_text(yaml.safe_dump(gc), encoding="utf-8")


def retire_kb(name: str) -> dict:
    """Archive-first removal (ROADMAP P11): MOVE the KB dir to
    RETIRED_DIR/<name>-<date>/ — no data is deleted, restore = move it
    back. The caller (API layer) is responsible for refusing while the
    KB has queued/running jobs."""
    kb_dir = resolve_kb(name)
    config.RETIRED_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d")
    dest = config.RETIRED_DIR / f"{name}-{stamp}"
    n = 1
    while dest.exists():
        n += 1
        dest = config.RETIRED_DIR / f"{name}-{stamp}-{n}"
    shutil.move(str(kb_dir), str(dest))
    _scrub_global_registry(str(kb_dir))
    return {"name": name, "retired_to": str(dest)}
