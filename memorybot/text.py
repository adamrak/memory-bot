"""Text helpers for Discord replies."""
import re
from collections.abc import Collection

from .config import DISCORD_LIMIT
from .paths import relative_path

ATTACH_LINE = re.compile(r"^\s*ATTACH:\s*(\S+)\s*$", re.MULTILINE)
MAX_ATTACH_PER_ANSWER = 3


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split into Discord-sized chunks, preferably at line breaks."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def extract_attach_lines(text: str, attachments: Collection[str]) -> tuple[str, list[str]]:
    """Remove 'ATTACH: path' lines from an answer; return the cleaned text and existing attachment paths."""
    paths = []
    for raw in ATTACH_LINE.findall(text):
        path = relative_path(raw)
        if path in attachments and path not in paths:
            paths.append(path)
    return ATTACH_LINE.sub("", text).strip(), paths[:MAX_ATTACH_PER_ANSWER]
