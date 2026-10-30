"""Path and link helpers. Paths are relative to MEMORY_DIR (e.g. "auto/stk.md") unless stated otherwise."""
import re
import unicodedata
from collections.abc import Collection, Iterable
from datetime import date

from .config import MEMORY_DIR

ATTACHMENT_LINK = re.compile(r"📎\s*\[[^\]]*\]\(([^)\s]+)\)")  # 📎 [name](/memory/path/file.pdf)


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", strip_accents(text).lower()).strip("-")[:60]


def relative_path(raw: str) -> str:
    """Strip quotes, leading slash and MEMORY_DIR prefix: '/memory/auto/stk.md' -> 'auto/stk.md'."""
    path = raw.strip().strip("`").replace("\\", "/").lstrip("/")
    if MEMORY_DIR and path.startswith(f"{MEMORY_DIR}/"):
        path = path[len(MEMORY_DIR) + 1:]
    return path


def sanitize_path(raw: str) -> str:
    """Normalize a note path: 'Elektronika/Akaso (GoPro)/Návod' -> 'elektronika/akaso-gopro/navod.md'."""
    path = relative_path(raw)
    if path.lower().endswith(".md"):
        path = path[:-3]
    parts = [slugify(p) for p in path.split("/") if p.strip() not in ("", ".")]
    if not parts or not all(parts):
        raise ValueError(f"Invalid path: {raw!r}")
    return "/".join(parts) + ".md"


def note_path(raw: str, notes: Collection[str]) -> str:
    """Keep exact paths of existing notes; normalize new ones."""
    rel = relative_path(raw)
    return rel if rel in notes else sanitize_path(raw)


def folder_of(path: str) -> str:
    return path.rsplit("/", 1)[0] + "/" if "/" in path else ""


def basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def planned_attachment_path(note: str, name: str, ext: str, today: date | None = None) -> str:
    """Attachments live next to their note: elektronika/akaso/akaso.md -> elektronika/akaso/2026-10-30-navod.pdf"""
    return f"{folder_of(note)}{today or date.today()}-{slugify(name) or 'file'}{ext}"


def link_target(path: str) -> str:
    """Repo-root link used inside notes; GitHub renders it as a working link."""
    return f"/{MEMORY_DIR}/{path}" if MEMORY_DIR else f"/{path}"


def refs_in(texts: Iterable[str]) -> set[str]:
    """Attachment paths linked via 📎 lines in the given texts."""
    return {relative_path(ref) for text in texts for ref in ATTACHMENT_LINK.findall(text)}


def rewrite_links(text: str, moves: dict[str, str]) -> str:
    """Point 📎 links of moved attachments to their new location."""
    def replace(m: re.Match) -> str:
        new = moves.get(relative_path(m.group(1)))
        return m.group(0).replace(m.group(1), link_target(new)) if new else m.group(0)

    return ATTACHMENT_LINK.sub(replace, text)
