"""PDF probing: the stage-1 "what kind of document is this" check.

Reproduces the manual walkthrough's first step (see PLAN.md): page count,
how many pages carry a real text layer, a sample of extractable text,
embedded-image resolution — enough for the UI to verdict "plain openkb
add" vs "OCR pipeline" and to guess the language.
"""

from pathlib import Path

import pymupdf

from . import config

# Pages with less extractable text than this are treated as image-only;
# scans often carry a few stray characters of junk text.
MIN_TEXT_CHARS = 40

# Tiny stopword-frequency language guesser — no extra dependency, and it
# only has to distinguish the languages this machine's KBs actually see.
_STOPWORDS = {
    "en": {"the", "and", "of", "to", "in", "is", "that", "for", "with", "was"},
    "es": {"el", "la", "de", "que", "los", "las", "una", "por", "con", "para"},
    "ca": {"els", "les", "amb", "del", "una", "que", "per", "com", "és", "dels"},
    "fr": {"le", "les", "des", "une", "est", "dans", "pour", "que", "avec", "sur"},
    "de": {"der", "die", "das", "und", "ist", "von", "mit", "den", "für", "auf"},
    "it": {"il", "di", "che", "per", "una", "con", "del", "gli", "della", "sono"},
}


def guess_language(text: str) -> str | None:
    words = [w.strip(".,;:()[]«»\"'").lower() for w in text.split()]
    if len(words) < 20:
        return None
    scores = {
        lang: sum(1 for w in words if w in stops)
        for lang, stops in _STOPWORDS.items()
    }
    best = max(scores, key=scores.get)
    # Require a real signal, and a margin over the runner-up — Catalan and
    # Spanish share enough short words that a near-tie means "unsure".
    ranked = sorted(scores.values(), reverse=True)
    if ranked[0] < 3 or (len(ranked) > 1 and ranked[0] < ranked[1] * 1.5):
        return None
    return best


def allowed_pdf_path(pdf: str) -> Path:
    """Resolve a client-supplied path, restricted to the inbox dir and
    KB raw/ dirs. Raises ValueError otherwise."""
    p = Path(pdf).expanduser().resolve()
    if not p.is_file() or p.suffix.lower() != ".pdf":
        raise ValueError(f"not a PDF file: {pdf}")
    allowed_roots = [config.INBOX_DIR.resolve()]
    for d in config.KB_ROOT.iterdir():
        if d.is_dir() and config.state_dir(d).is_dir():
            allowed_roots.append((d / "raw").resolve())
    if not any(p.is_relative_to(root) for root in allowed_roots):
        raise ValueError(f"path outside inbox and KB raw/ dirs: {pdf}")
    return p


def render_page_png(pdf: str, page: int, width: int = 900) -> bytes:
    """Render one page to PNG for the pilot review's side-by-side view."""
    path = allowed_pdf_path(pdf)
    doc = pymupdf.open(path)
    try:
        if not 1 <= page <= doc.page_count:
            raise ValueError(f"page {page} out of range 1-{doc.page_count}")
        pg = doc[page - 1]
        zoom = width / pg.rect.width if pg.rect.width else 1.0
        pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        return pix.tobytes("png")
    finally:
        doc.close()


def probe_pdf(pdf: str) -> dict:
    path = allowed_pdf_path(pdf)
    doc = pymupdf.open(path)
    try:
        page_count = doc.page_count
        text_pages = 0
        sample_text = None
        sample_page = None
        all_text_parts = []
        for i in range(page_count):
            text = doc[i].get_text().strip()
            if len(text) >= MIN_TEXT_CHARS:
                text_pages += 1
                if sample_text is None:
                    sample_text = text[:600]
                    sample_page = i + 1
                if len(all_text_parts) < 10:
                    all_text_parts.append(text[:1000])

        # Embedded-image resolution from the first few pages that have
        # images: report the largest image and its effective DPI.
        image_info = None
        pages_checked = 0
        for i in range(page_count):
            images = doc[i].get_images(full=True)
            if not images:
                continue
            pages_checked += 1
            rect = doc[i].rect
            for img in images:
                width_px, height_px = img[2], img[3]
                if image_info is None or width_px * height_px > (
                    image_info["width_px"] * image_info["height_px"]
                ):
                    dpi = round(width_px / (rect.width / 72)) if rect.width else None
                    image_info = {
                        "page": i + 1,
                        "width_px": width_px,
                        "height_px": height_px,
                        "approx_dpi": dpi,
                    }
            if pages_checked >= 5:
                break

        if text_pages == 0:
            verdict = "scan"
        elif text_pages >= page_count * 0.8:
            verdict = "text"
        else:
            verdict = "mixed"

        return {
            "pdf": str(path),
            "name": path.name,
            "page_count": page_count,
            "text_pages": text_pages,
            "verdict": verdict,
            "sample_page": sample_page,
            "sample_text": sample_text,
            "language_guess": guess_language(" ".join(all_text_parts)),
            "largest_image": image_info,
        }
    finally:
        doc.close()
