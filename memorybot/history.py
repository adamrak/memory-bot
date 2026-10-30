"""Turning a Discord thread into Gemini conversation contents."""
import asyncio
import mimetypes
import os
from dataclasses import dataclass

import discord
from google.genai import types

from .config import HISTORY_LIMIT, HISTORY_MAX_CHARS, INLINE_MIME, MAX_INLINE_BYTES, MAX_MEDIA
from .files import is_photo, prepare_file

USER_MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)


@dataclass
class ThreadAttachment:
    ref: str  # "A1", "A2", … in chronological order
    attachment: discord.Attachment

    @property
    def mime(self) -> str:
        mime = self.attachment.content_type or mimetypes.guess_type(self.attachment.filename)[0] or ""
        return mime.split(";")[0].strip().lower()

    @property
    def predicted_ext(self) -> str:
        return ".jpg" if is_photo(self.mime) else os.path.splitext(self.attachment.filename)[1].lower() or ".bin"

    def describe(self) -> str:
        return f"{self.ref}: {self.attachment.filename} ({self.mime or 'unknown type'}, {self.attachment.size / 1e6:.1f} MB)"


async def build_history(
    thread: discord.Thread, bot_user_id: int, include_pdfs: bool
) -> tuple[list[types.Content], list[ThreadAttachment]]:
    """Thread messages as Gemini contents (newest within budget) + all user attachments in the thread.
    Photos are always sent inline; PDFs only when the user explicitly asks to read them."""
    msgs = [m async for m in thread.history(limit=HISTORY_LIMIT)]
    msgs.reverse()
    if len(msgs) < HISTORY_LIMIT:  # thread was started from a channel message -> include it
        starter = thread.starter_message
        if starter is None and thread.parent:
            try:
                starter = await thread.parent.fetch_message(thread.id)
            except discord.HTTPException:
                starter = None
        if starter:
            msgs.insert(0, starter)

    msgs = [m for m in msgs if m.type in USER_MESSAGE_TYPES and (m.clean_content or m.attachments)]

    # Keep the newest messages within the char budget
    kept, total = [], 0
    for m in reversed(msgs):
        total += len(m.clean_content)
        if total > HISTORY_MAX_CHARS and kept:
            break
        kept.append(m)
    kept.reverse()

    atts: list[ThreadAttachment] = []
    by_msg: dict[int, list[ThreadAttachment]] = {}
    for m in kept:
        if m.author.id != bot_user_id:
            for a in m.attachments:
                t = ThreadAttachment(f"A{len(atts) + 1}", a)
                atts.append(t)
                by_msg.setdefault(m.id, []).append(t)

    inline = [
        t for t in atts
        if t.mime in INLINE_MIME
        and (include_pdfs or t.mime != "application/pdf")
        and t.attachment.size <= MAX_INLINE_BYTES
    ][-MAX_MEDIA:]

    async def load(t: ThreadAttachment) -> tuple[bytes, str]:
        data = await t.attachment.read()
        if t.mime.startswith("image/"):  # shrink photos before sending them to Gemini
            data, ext = await asyncio.to_thread(prepare_file, data, t.attachment.filename, t.mime)
            if ext == ".jpg":
                return data, "image/jpeg"
        return data, t.mime

    results = await asyncio.gather(*(load(t) for t in inline), return_exceptions=True)
    loaded = {t.ref: r for t, r in zip(inline, results) if isinstance(r, tuple)}

    contents: list[types.Content] = []
    for m in kept:
        role = "model" if m.author.id == bot_user_id else "user"
        parts = [types.Part.from_text(text=m.clean_content)] if m.clean_content else []
        for t in by_msg.get(m.id, []):
            if t.ref in loaded:
                data, mime = loaded[t.ref]
                parts.append(types.Part.from_text(text=f"[attachment {t.ref}: {t.attachment.filename}]"))
                parts.append(types.Part.from_bytes(data=data, mime_type=mime))
            else:
                parts.append(types.Part.from_text(text=f"[attachment {t.ref}: {t.attachment.filename} – not read]"))
        if not parts:
            continue
        if contents and contents[-1].role == role:
            contents[-1].parts.extend(parts)
        else:
            contents.append(types.Content(role=role, parts=parts))
    while contents and contents[0].role == "model":
        contents.pop(0)
    return contents, atts
