import asyncio
import base64
import difflib
import io
import logging
import mimetypes
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Awaitable, Callable, Literal
from urllib.parse import quote

import aiohttp
import discord
import httpx
from aiohttp import web
from discord.ext import commands, tasks
from google import genai
from google.genai import errors, types
from PIL import Image, ImageOps
from pydantic import BaseModel

log = logging.getLogger("memorybot")

# --- Config (environment variables) ---
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
_owner = os.environ["OWNER_ID"].strip()
if not _owner.isdigit():
    raise SystemExit(f"OWNER_ID must be your numeric Discord user ID (Developer Mode → Copy User ID), got {_owner!r}")
OWNER_ID = int(_owner)
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))  # optional: only this channel + its threads
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]  # "owner/repo"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "")  # empty = repo default branch
MEMORY_DIR = os.getenv("MEMORY_DIR", "memory").strip("/")  # notes live here; empty = repo root
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
# Comma-separated, tried in order; next one is used when the previous times out / returns 5xx
GEMINI_MODELS = [m.strip() for m in os.getenv("GEMINI_MODEL", "gemini-3.5-flash").split(",") if m.strip()]
GEMINI_THINKING = os.getenv("GEMINI_THINKING", "low").strip()  # "low" / "high" / empty = model default
GEMINI_TIMEOUT_S = int(os.getenv("GEMINI_TIMEOUT_S", "45"))
SYNC_MINUTES = float(os.getenv("SYNC_MINUTES", "5"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "50"))
HISTORY_MAX_CHARS = int(os.getenv("HISTORY_MAX_CHARS", "40000"))
MAX_MEDIA = int(os.getenv("MAX_MEDIA", "5"))  # newest N thread images/PDFs sent to Gemini
MAX_ATTACHMENT_MB = int(os.getenv("MAX_ATTACHMENT_MB", "25"))
PORT = os.getenv("PORT")  # set by Render -> starts keep-alive HTTP server

GITHUB_API = "https://api.github.com"
DISCORD_LIMIT = 2000
DISCORD_FILE_LIMIT = 10 * 1024 * 1024  # bot upload limit; bigger files are sent as GitHub links
MAX_INLINE_BYTES = 15 * 1024 * 1024  # per file sent inline to Gemini
MAX_ATTACHMENT_BYTES = MAX_ATTACHMENT_MB * 1024 * 1024
MAX_IMAGE_PX = 2048
EDIT_DIFF_CHARS = 1400
INLINE_MIME = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "application/pdf"}
USER_MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)
SAVE_TRIGGER = re.compile(r"^\s*(zapis|zapisat|zapisu|uloz|ulozit|save)\b", re.IGNORECASE)
EDIT_TRIGGER = re.compile(
    r"^\s*(oprav|opravit|uprav|upravit|zmaz|zmazat|vymaz|vymazat|presun|presunut|edit|delete|remove|move)\b",
    re.IGNORECASE,
)
ATTACH_LINE = re.compile(r"^\s*ATTACH:\s*(\S+)\s*$", re.MULTILINE)
ATTACHMENT_LINK = re.compile(
    r"\]\(" + (rf"/?(?:{re.escape(MEMORY_DIR)}/)?" if MEMORY_DIR else "/?") + r"(attachments/[^)\s]+)\)"
)

ANSWER_PROMPT = """You are a personal memory assistant for a single user. Below are their notes \
from a private GitHub repository (markdown files).

Rules:
- Answer primarily from the notes. When you use a note, cite its path like (📁 path/to/file.md).
- If the notes do not contain the answer, say so clearly first. You may then add general knowledge, \
explicitly labelled as not coming from the notes.
- Never invent personal facts (models, dates, places, people, numbers) that are not in the notes.
- Images/PDFs the user sent in this conversation are included inline; use them.
- Notes may link stored files (lines starting with 📎). If the user asks to see a stored file, or a stored \
photo is essential for the answer (e.g. where something is), add at the very end one line per file: \
ATTACH: attachments/<path> (max 3). The bot sends those files. Never mention this mechanism.
- Reply in the language the user writes in. Be concise. Use Discord markdown.

=== NOTES ({count} files) ===
{notes}
=== END OF NOTES ===

Today is {today}."""

SAVE_PROMPT = """The user wants to save something into their personal notes repository. \
Based on the conversation, decide WHAT to save and WHERE.

Content:
- If they ask to save a summary / the conversation / "this" / "it", write a concise markdown summary \
of the useful facts and conclusions from the conversation. Facts only, no chit-chat.
- If they dictate a specific fact, save it faithfully, only lightly cleaned up.
- For documents (receipts, warranty cards, manuals) save the main data only: what it is, brand/vendor, \
model, serial number, date, price, warranty end; for manuals also key specs, the most useful procedures \
and a short table of contents with page numbers.
- For photos of a place or setup, describe precisely where things are and how they look.
- Start the entry with a heading line: "### {today} – <short title>".
- Do not add links to attachments; the bot adds them.
- Write in the language of the conversation.

Path:
- If the user named a section/file (e.g. "do sekcie auto/stk"), use exactly that as the path \
and set path_from_user=true.
- Otherwise pick the best fitting existing file from the list below, or propose a new path \
"<topic>/<subject>.md", e.g. auto/stk.md, domacnost/pracka.md, zdravie/ockovanie.md \
(lowercase, kebab-case, no diacritics, reuse existing topic folders). Set path_from_user=false.

Attachments:
- Attachments in this conversation are listed below (ref: filename, type, size).
- If the user wants to save some of them ("zapíš si to" right after a photo, "tú fotku", "všetky účtenky"), \
list them with a short descriptive kebab-case name without extension or date, e.g. zarucak-pracka. \
Otherwise return an empty list.

Existing notes:
{tree}

Attachments in this conversation:
{attachments}"""

EDIT_PROMPT = """The user wants to edit, move or delete something in their notes. All notes are below.

Return the minimal set of file changes:
- action "update": path + the COMPLETE new content of the file. Keep unchanged parts verbatim.
- action "delete": path, when the whole file should be removed (new_content empty).
- To move content, update/delete the source file and update/create the target file.
- Do not touch files that don't need to change.
- Keep attachment links (lines starting with 📎) together with the facts they belong to; \
remove them only when removing that fact.
- summary: one short sentence describing the change, in the user's language.
- If the request is unclear or nothing matches, return no changes and explain why in summary.
- Paths are relative, e.g. domacnost/pracka.md.

=== NOTES ===
{notes}
=== END OF NOTES ===

Today is {today}."""


class AttachmentPick(BaseModel):
    ref: str
    name: str


class SavePlan(BaseModel):
    path: str
    path_from_user: bool
    content: str
    attachments: list[AttachmentPick]


class FileChange(BaseModel):
    path: str
    action: Literal["update", "delete"]
    new_content: str


class EditPlan(BaseModel):
    summary: str
    changes: list[FileChange]


# --- Helpers ---
def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", strip_accents(text).lower()).strip("-")[:60]


def is_save_command(text: str) -> bool:
    return bool(SAVE_TRIGGER.match(strip_accents(text)))


def is_edit_command(text: str) -> bool:
    return bool(EDIT_TRIGGER.match(strip_accents(text)))


def relative_path(raw: str) -> str:
    """Strip quotes, leading slash and MEMORY_DIR prefix: '/memory/auto/stk.md' -> 'auto/stk.md'."""
    path = raw.strip().strip("`").replace("\\", "/").lstrip("/")
    if MEMORY_DIR and path.startswith(f"{MEMORY_DIR}/"):
        path = path[len(MEMORY_DIR) + 1:]
    return path


def sanitize_path(raw: str) -> str:
    """Normalize a note path relative to MEMORY_DIR, e.g. 'auto/stk.md'."""
    parts = [p.strip() for p in relative_path(raw).split("/")]
    parts = [p for p in parts if p and p != "."]
    if not parts or any(p == ".." or p.startswith(".") for p in parts):
        raise ValueError(f"Invalid path: {raw!r}")
    path = "/".join(parts)
    return path if path.lower().endswith(".md") else f"{path}.md"


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
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


def link_target(path: str) -> str:
    """Repo-root link used inside notes; GitHub renders it as a working link."""
    return f"/{MEMORY_DIR}/{path}" if MEMORY_DIR else f"/{path}"


def refs_in(texts) -> set[str]:
    return {ref for text in texts for ref in ATTACHMENT_LINK.findall(text)}


def topic_of(note_path: str) -> str:
    parts = note_path.split("/")
    return parts[0] if len(parts) > 1 else "misc"


def planned_attachment_path(note_path: str, name: str, ext: str) -> str:
    return f"attachments/{topic_of(note_path)}/{date.today()}-{slugify(name) or 'file'}{ext}"


def prepare_file(data: bytes, filename: str, mime: str) -> tuple[bytes, str]:
    """Shrink photos to max 2048 px JPEG; keep everything else as-is. Returns (data, extension)."""
    ext = os.path.splitext(filename)[1].lower() or ".bin"
    if mime.startswith("image/") and mime != "image/gif":
        try:
            with Image.open(io.BytesIO(data)) as img:
                img = ImageOps.exif_transpose(img)
                img.thumbnail((MAX_IMAGE_PX, MAX_IMAGE_PX))
                out = io.BytesIO()
                img.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
                return out.getvalue(), ".jpg"
        except Exception:
            log.warning("Could not re-encode %s, storing original", filename)
    return data, ext


@dataclass
class ThreadAttachment:
    ref: str
    attachment: discord.Attachment

    @property
    def mime(self) -> str:
        mime = self.attachment.content_type or mimetypes.guess_type(self.attachment.filename)[0] or ""
        return mime.split(";")[0].strip().lower()

    @property
    def predicted_ext(self) -> str:
        if self.mime.startswith("image/") and self.mime != "image/gif":
            return ".jpg"
        return os.path.splitext(self.attachment.filename)[1].lower() or ".bin"

    def describe(self) -> str:
        return f"{self.ref}: {self.attachment.filename} ({self.mime or 'unknown type'}, {self.attachment.size / 1e6:.1f} MB)"


# --- GitHub-backed memory store ---
class GitHubError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"GitHub {status}: {text}")
        self.status = status


class ConflictError(Exception):
    pass


class MemoryStore:
    """In-memory copy of all notes under MEMORY_DIR (+ list of attachments). GitHub is the source of truth."""

    def __init__(self):
        self.files: dict[str, str] = {}  # note path -> content
        self.attachments: dict[str, tuple[str, int]] = {}  # attachment path -> (blob sha, size)
        self.sha: str | None = None  # commit the cache reflects
        self.tree_sha: str | None = None
        self.branch = GITHUB_BRANCH
        self._md_sha: dict[str, str] = {}
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def start(self):
        self._session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=aiohttp.ClientTimeout(total=120),
        )

    async def close(self):
        if self._session:
            await self._session.close()

    def repo_path(self, path: str) -> str:
        return f"{MEMORY_DIR}/{path}" if MEMORY_DIR else path

    def blob_url(self, path: str) -> str:
        return f"https://github.com/{GITHUB_REPO}/blob/{quote(self.branch)}/{quote(self.repo_path(path))}"

    async def _request(self, method: str, url: str, *, accept: str | None = None, raw: bool = False, **kwargs):
        headers = {"Accept": accept} if accept else None
        async with self._session.request(
            method, f"{GITHUB_API}/repos/{GITHUB_REPO}{url}", headers=headers, **kwargs
        ) as r:
            if r.status >= 400:
                raise GitHubError(r.status, (await r.text())[:300])
            return await r.read() if raw else await r.json()

    async def read_blob(self, sha: str) -> bytes:
        return await self._request("GET", f"/git/blobs/{sha}", accept="application/vnd.github.raw+json", raw=True)

    async def sync(self) -> bool:
        async with self._lock:
            return await self._sync_locked()

    async def _sync_locked(self) -> bool:
        """Reload only notes whose blob changed since the last sync. Returns True if the head moved."""
        if not self.branch:
            self.branch = (await self._request("GET", ""))["default_branch"]
        head = (
            await self._request(
                "GET", f"/commits/{quote(self.branch)}", accept="application/vnd.github.sha", raw=True
            )
        ).decode().strip()
        if head == self.sha:
            return False

        tree = await self._request("GET", f"/git/trees/{head}", params={"recursive": "1"})
        prefix = f"{MEMORY_DIR}/" if MEMORY_DIR else ""
        md_sha: dict[str, str] = {}
        attachments: dict[str, tuple[str, int]] = {}
        for e in tree["tree"]:
            if e["type"] != "blob" or not e["path"].startswith(prefix):
                continue
            path = e["path"][len(prefix):]
            if path.startswith("attachments/"):
                attachments[path] = (e["sha"], e.get("size", 0))
            elif path.lower().endswith(".md"):
                md_sha[path] = e["sha"]

        changed = [p for p, s in md_sha.items() if self._md_sha.get(p) != s]
        semaphore = asyncio.Semaphore(8)

        async def fetch(path: str) -> tuple[str, str]:
            async with semaphore:
                return path, (await self.read_blob(md_sha[path])).decode("utf-8", errors="replace")

        fetched = dict(await asyncio.gather(*(fetch(p) for p in changed)))
        self.files = {p: fetched[p] if p in fetched else self.files[p] for p in md_sha}
        self._md_sha, self.attachments = md_sha, attachments
        self.sha, self.tree_sha = head, tree["sha"]
        log.info("Synced %d notes, %d attachments @ %s (%d notes fetched)",
                 len(self.files), len(self.attachments), head[:7], len(changed))
        return True

    async def apply(self, build: Callable[[], dict[str, str | bytes | None]], message: str) -> str:
        """Run build() against fresh state and commit its result atomically.
        build returns {path: text | bytes (binary) | None (delete)}. Returns the commit URL."""
        async with self._lock:
            for attempt in range(3):
                await self._sync_locked()
                changes = build()
                if not changes:
                    raise ValueError("Nothing to change.")
                entries = []
                for path, value in changes.items():
                    entry = {"path": self.repo_path(path), "mode": "100644", "type": "blob"}
                    if value is None:
                        entry["sha"] = None
                    elif isinstance(value, bytes):
                        blob = await self._request(
                            "POST", "/git/blobs",
                            json={"content": base64.b64encode(value).decode("ascii"), "encoding": "base64"},
                        )
                        entry["sha"] = blob["sha"]
                    else:
                        entry["content"] = value
                    entries.append(entry)
                tree = await self._request("POST", "/git/trees", json={"base_tree": self.tree_sha, "tree": entries})
                commit = await self._request(
                    "POST", "/git/commits", json={"message": message, "tree": tree["sha"], "parents": [self.sha]}
                )
                try:
                    await self._request("PATCH", f"/git/refs/heads/{self.branch}", json={"sha": commit["sha"]})
                except GitHubError as e:
                    if e.status == 422 and attempt < 2:  # branch moved meanwhile -> rebuild on top of it
                        continue
                    raise
                await self._sync_locked()
                return f"https://github.com/{GITHUB_REPO}/commit/{commit['sha']}"
        raise ConflictError("Repository keeps changing, try again.")


store = MemoryStore()
gemini = genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_S * 1000))


# --- Gemini ---
def make_config(**kwargs) -> types.GenerateContentConfig:
    if GEMINI_THINKING:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=GEMINI_THINKING)
    return types.GenerateContentConfig(
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        **kwargs,
    )


async def generate(contents, config: types.GenerateContentConfig) -> types.GenerateContentResponse:
    """Try models in order; fall back to the next one on server errors / timeouts."""
    last_error: Exception | None = None
    for model in GEMINI_MODELS:
        start = time.monotonic()
        try:
            resp = await gemini.aio.models.generate_content(model=model, contents=contents, config=config)
            log.info("Gemini %s answered in %.1fs", model, time.monotonic() - start)
            return resp
        except (errors.ServerError, httpx.TimeoutException) as e:
            log.warning("Gemini %s failed after %.1fs: %s", model, time.monotonic() - start, e)
            last_error = e
    raise last_error


def parse_json(resp: types.GenerateContentResponse, model_cls):
    return resp.parsed if isinstance(resp.parsed, model_cls) else model_cls.model_validate_json(resp.text or "")


def notes_text() -> str:
    return "\n".join(f"\n--- FILE: {p} ---\n{t}" for p, t in sorted(store.files.items())) or "(no notes yet)"


async def gemini_answer(history: list[types.Content]) -> str:
    system = ANSWER_PROMPT.format(count=len(store.files), notes=notes_text(), today=date.today())
    resp = await generate(history, make_config(system_instruction=system))
    return resp.text or "⚠️ Gemini returned an empty response (possibly blocked by safety filters)."


async def gemini_save_plan(history: list[types.Content], atts: list[ThreadAttachment]) -> SavePlan:
    system = SAVE_PROMPT.format(
        today=date.today(),
        tree="\n".join(sorted(store.files)) or "(no notes yet)",
        attachments="\n".join(t.describe() for t in atts) or "(none)",
    )
    resp = await generate(
        history,
        make_config(system_instruction=system, response_mime_type="application/json", response_schema=SavePlan),
    )
    plan = parse_json(resp, SavePlan)
    try:
        plan.path = sanitize_path(plan.path)
    except ValueError:
        plan.path, plan.path_from_user = "inbox.md", False
    return plan


async def gemini_edit_plan(history: list[types.Content]) -> EditPlan:
    system = EDIT_PROMPT.format(notes=notes_text(), today=date.today())
    resp = await generate(
        history,
        make_config(system_instruction=system, response_mime_type="application/json", response_schema=EditPlan),
    )
    return parse_json(resp, EditPlan)


# --- Thread history ---
async def build_history(thread: discord.Thread) -> tuple[list[types.Content], list[ThreadAttachment]]:
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

    # Number user attachments chronologically (A1, A2, …); load the newest few inline for Gemini
    atts: list[ThreadAttachment] = []
    by_msg: dict[int, list[ThreadAttachment]] = {}
    for m in kept:
        if m.author.id != bot.user.id:
            for a in m.attachments:
                t = ThreadAttachment(f"A{len(atts) + 1}", a)
                atts.append(t)
                by_msg.setdefault(m.id, []).append(t)
    inline = [t for t in atts if t.mime in INLINE_MIME and t.attachment.size <= MAX_INLINE_BYTES][-MAX_MEDIA:]
    results = await asyncio.gather(*(t.attachment.read() for t in inline), return_exceptions=True)
    loaded = {t.ref: r for t, r in zip(inline, results) if isinstance(r, bytes)}

    contents: list[types.Content] = []
    for m in kept:
        role = "model" if m.author.id == bot.user.id else "user"
        parts = [types.Part.from_text(text=m.clean_content)] if m.clean_content else []
        for t in by_msg.get(m.id, []):
            if t.ref in loaded:
                parts.append(types.Part.from_text(text=f"[attachment {t.ref}: {t.attachment.filename}]"))
                parts.append(types.Part.from_bytes(data=loaded[t.ref], mime_type=t.mime))
            else:
                parts.append(types.Part.from_text(text=f"[attachment {t.ref}: {t.attachment.filename} – not loaded]"))
        if not parts:
            continue
        if contents and contents[-1].role == role:
            contents[-1].parts.extend(parts)
        else:
            contents.append(types.Content(role=role, parts=parts))
    while contents and contents[0].role == "model":
        contents.pop(0)
    return contents, atts


# --- Save / edit logic ---
async def save_note(plan: SavePlan, picks: list[tuple[ThreadAttachment, str]]) -> str:
    processed = []
    for t, name in picks:
        if t.attachment.size > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"{t.attachment.filename} is larger than {MAX_ATTACHMENT_MB} MB")
        data, ext = await asyncio.to_thread(prepare_file, await t.attachment.read(), t.attachment.filename, t.mime)
        processed.append((name, ext, data))

    def build() -> dict[str, str | bytes | None]:
        changes: dict[str, str | bytes | None] = {}
        links = []
        for name, ext, data in processed:
            base = planned_attachment_path(plan.path, name, "")
            path, i = f"{base}{ext}", 2
            while path in store.attachments or path in changes:
                path, i = f"{base}-{i}{ext}", i + 1
            changes[path] = data
            links.append(f"📎 [{path.rsplit('/', 1)[1]}]({link_target(path)})")
        entry = plan.content.strip()
        if links:
            entry += "\n\n" + "\n".join(links)
        existing = store.files.get(plan.path)
        changes[plan.path] = f"{existing.rstrip()}\n\n{entry}\n" if existing else f"{entry}\n"
        return changes

    return await store.apply(build, f"Save {store.repo_path(plan.path)} via Discord memory bot")


def prepare_edit(plan: EditPlan) -> tuple[dict[str, str | None], dict[str, str | None], list[str]]:
    """Returns (changes, base snapshot, orphaned attachments)."""
    changes: dict[str, str | None] = {}
    base: dict[str, str | None] = {}
    for c in plan.changes:
        try:
            path = sanitize_path(c.path)
        except ValueError:
            continue
        if path.startswith("attachments/"):
            continue  # attachments are handled by the bot, not by Gemini
        old = store.files.get(path)
        new = None if c.action == "delete" else c.new_content.rstrip() + "\n"
        if (old is None and new is None) or (old is not None and new is not None and old.rstrip() == new.rstrip()):
            continue
        changes[path], base[path] = new, old
    after = {**store.files, **changes}
    linked_after = refs_in(v for v in after.values() if v is not None)
    orphans = sorted((refs_in(store.files.values()) - linked_after) & store.attachments.keys())
    return changes, base, orphans


def render_diff(changes: dict[str, str | None], base: dict[str, str | None]) -> str:
    out = []
    for path, new in changes.items():
        old = base[path]
        out.extend(difflib.unified_diff(
            (old or "").splitlines(), (new or "").splitlines(),
            f"a/{path}" if old is not None else "/dev/null",
            f"b/{path}" if new is not None else "/dev/null",
            lineterm="", n=1,
        ))
    return "\n".join(out)


async def send_stored_files(thread: discord.Thread, paths: list[str]):
    files, links = [], []
    for path in paths:
        sha, size = store.attachments[path]
        name = path.rsplit("/", 1)[1]
        if size > DISCORD_FILE_LIMIT:
            links.append(f"📎 [{name}](<{store.blob_url(path)}>)")
        else:
            files.append(discord.File(io.BytesIO(await store.read_blob(sha)), filename=name))
    kwargs = {}
    if links:
        kwargs["content"] = "\n".join(links)
    if files:
        kwargs["files"] = files
    if kwargs:
        await thread.send(**kwargs)


def extract_attach_lines(text: str) -> tuple[str, list[str]]:
    paths = []
    for raw in ATTACH_LINE.findall(text):
        path = relative_path(raw)
        if path in store.attachments and path not in paths:
            paths.append(path)
    return ATTACH_LINE.sub("", text).strip(), paths[:3]


# --- Discord UI ---
class OwnerView(discord.ui.View):
    default_status = ""

    def __init__(self):
        super().__init__(timeout=900)
        self.message: discord.Message | None = None

    def render(self, status: str | None = None) -> str:
        raise NotImplementedError

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == OWNER_ID

    async def run_commit(self, interaction: discord.Interaction, busy: str, action: Callable[[], Awaitable[str]]):
        await interaction.response.edit_message(content=self.render(busy), view=None)
        try:
            url = await action()
        except Exception as e:
            log.exception("Commit failed")
            await interaction.edit_original_response(content=self.render(f"❌ Failed: {str(e)[:300]}"), view=self)
            return
        self.stop()
        await interaction.edit_original_response(content=self.render(f"✅ Done · [commit](<{url}>)"), view=None)

    async def cancel_action(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(content=self.render("❌ Cancelled, nothing changed."), view=None)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content=self.render("⌛ Expired, nothing changed."), view=None)
            except discord.HTTPException:
                pass


class SaveView(OwnerView):
    def __init__(self, plan: SavePlan, picks: list[tuple[ThreadAttachment, str]]):
        super().__init__()
        self.plan = plan
        self.picks = picks
        self.selected = {t.ref for t, _ in picks}
        if picks:
            self.add_item(FileSelect(self))

    def render(self, status: str | None = None) -> str:
        origin = "your path" if self.plan.path_from_user else "suggested"
        lines = [status or "💾 Save this?", f"📁 `{self.plan.path}` ({origin})"]
        for t, name in self.picks:
            mark = "☑" if t.ref in self.selected else "☐"
            target = planned_attachment_path(self.plan.path, name, t.predicted_ext)
            lines.append(f"{mark} 📎 {t.attachment.filename} → `{target}`")
        head = "\n".join(lines)
        budget = max(DISCORD_LIMIT - len(head) - 20, 100)
        body = self.plan.content.replace("```", "'''")
        if len(body) > budget:
            body = body[: budget - 2] + "\n…"
        return f"{head}\n```md\n{body}\n```"

    @discord.ui.button(label="Save", emoji="✅", style=discord.ButtonStyle.success, row=0)
    async def save(self, interaction: discord.Interaction, _button: discord.ui.Button):
        picks = [(t, name) for t, name in self.picks if t.ref in self.selected]
        await self.run_commit(interaction, "⏳ Saving…", lambda: save_note(self.plan, picks))

    @discord.ui.button(label="Change path", emoji="📝", style=discord.ButtonStyle.secondary, row=0)
    async def change_path(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_modal(PathModal(self))

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.danger, row=0)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cancel_action(interaction)


class FileSelect(discord.ui.Select):
    def __init__(self, save_view: SaveView):
        options = [
            discord.SelectOption(label=t.attachment.filename[:100], value=t.ref, default=True)
            for t, _ in save_view.picks
        ]
        super().__init__(placeholder="Files to save", min_values=0, max_values=len(options), options=options, row=1)
        self.save_view = save_view

    async def callback(self, interaction: discord.Interaction):
        self.save_view.selected = set(self.values)
        for option in self.options:
            option.default = option.value in self.save_view.selected
        await interaction.response.edit_message(content=self.save_view.render(), view=self.save_view)


class PathModal(discord.ui.Modal, title="Change path"):
    def __init__(self, save_view: SaveView):
        super().__init__()
        self.save_view = save_view
        self.path_input = discord.ui.TextInput(
            label="Path (e.g. auto/stk)", default=save_view.plan.path, max_length=200
        )
        self.add_item(self.path_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            path = sanitize_path(self.path_input.value)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        self.save_view.plan.path = path
        self.save_view.plan.path_from_user = True
        await interaction.response.edit_message(content=self.save_view.render(), view=self.save_view)


class EditView(OwnerView):
    def __init__(self, plan: EditPlan):
        super().__init__()
        self.plan = plan
        self.changes, self.base, self.orphans = prepare_edit(plan)
        self.diff = render_diff(self.changes, self.base)

    @property
    def diff_truncated(self) -> bool:
        return len(self.diff) > EDIT_DIFF_CHARS

    def render(self, status: str | None = None) -> str:
        lines = [status or "✏️ Apply these changes?", self.plan.summary[:300]]
        lines += [f"🗑 `{p}` (attachment no longer linked)" for p in self.orphans[:5]]
        if len(self.orphans) > 5:
            lines.append(f"🗑 … and {len(self.orphans) - 5} more attachments")
        diff = self.diff.replace("```", "'''")
        if self.diff_truncated:
            diff = diff[:EDIT_DIFF_CHARS] + "\n… (full diff in changes.diff)"
        return "\n".join(lines) + f"\n```diff\n{diff}\n```"

    def build(self) -> dict[str, str | bytes | None]:
        for path, old in self.base.items():
            if store.files.get(path) != old:
                raise ConflictError(f"`{path}` changed since this preview, ask again.")
        changes: dict[str, str | bytes | None] = dict(self.changes)
        after = {**store.files, **self.changes}
        linked = refs_in(v for v in after.values() if v is not None)
        for path in self.orphans:
            if path in store.attachments and path not in linked:
                changes[path] = None
        return changes

    @discord.ui.button(label="Apply", emoji="✅", style=discord.ButtonStyle.success)
    async def apply(self, interaction: discord.Interaction, _button: discord.ui.Button):
        message = f"Edit via Discord memory bot: {self.plan.summary}"[:250]
        await self.run_commit(interaction, "⏳ Applying…", lambda: store.apply(self.build, message))

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cancel_action(interaction)


# --- Handlers ---
async def handle_answer(thread: discord.Thread, history: list[types.Content]):
    text, paths = extract_attach_lines(await gemini_answer(history))
    for chunk in split_message(text):
        await thread.send(chunk)
    if paths:
        await send_stored_files(thread, paths)


async def handle_save(thread: discord.Thread, history: list[types.Content], atts: list[ThreadAttachment]):
    plan = await gemini_save_plan(history, atts)
    by_ref = {t.ref: t for t in atts}
    picks, seen = [], set()
    for pick in plan.attachments:
        if pick.ref in by_ref and pick.ref not in seen:
            seen.add(pick.ref)
            picks.append((by_ref[pick.ref], pick.name))
    view = SaveView(plan, picks[:25])
    view.message = await thread.send(view.render(), view=view)


async def handle_edit(thread: discord.Thread, history: list[types.Content]):
    plan = await gemini_edit_plan(history)
    view = EditView(plan)
    if not view.changes:
        await thread.send(f"🤷 {plan.summary or 'Nothing to change.'}")
        return
    kwargs = {}
    if view.diff_truncated:
        kwargs["file"] = discord.File(io.BytesIO(view.diff.encode("utf-8")), filename="changes.diff")
    view.message = await thread.send(view.render(), view=view, **kwargs)


# --- Bot ---
async def start_health_server(port: int):
    async def health(_request):
        return web.Response(text=f"ok, {len(store.files)} notes @ {(store.sha or '-')[:7]}")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Health server listening on :%d", port)


@tasks.loop(minutes=SYNC_MINUTES)
async def periodic_sync():
    try:
        await store.sync()
    except Exception:
        log.exception("Periodic sync failed")


class MemoryBot(commands.Bot):
    async def setup_hook(self):
        await store.start()
        if PORT:
            await start_health_server(int(PORT))
        periodic_sync.start()  # first iteration runs immediately = initial sync

    async def close(self):
        periodic_sync.cancel()
        await store.close()
        await super().close()


intents = discord.Intents.default()
intents.message_content = True
bot = MemoryBot(command_prefix="!", intents=intents, owner_id=OWNER_ID)


@bot.event
async def on_ready():
    log.info("Logged in as %s (models: %s, thinking: %s)", bot.user, ", ".join(GEMINI_MODELS), GEMINI_THINKING or "default")


@bot.command(name="sync")
async def sync_cmd(ctx: commands.Context):
    try:
        changed = await store.sync()
    except Exception as e:
        await ctx.reply(f"❌ Sync failed: {e}")
        return
    state = "🔄 Synced" if changed else "✅ Already up to date"
    await ctx.reply(f"{state}: {len(store.files)} notes, {len(store.attachments)} attachments @ `{(store.sha or '-')[:7]}`")


@bot.command(name="ping")
async def ping_cmd(ctx: commands.Context):
    """Bare Gemini call (no notes, no history) against each configured model, with timing."""
    lines = []
    async with ctx.typing():
        for model in GEMINI_MODELS:
            start = time.monotonic()
            try:
                resp = await gemini.aio.models.generate_content(
                    model=model, contents="Reply with one word: hi", config=make_config()
                )
                lines.append(f"✅ `{model}` {time.monotonic() - start:.1f}s → {(resp.text or '').strip()[:50]!r}")
            except Exception as e:
                lines.append(f"❌ `{model}` {time.monotonic() - start:.1f}s → {type(e).__name__}: {str(e)[:150]}")
    await ctx.reply("\n".join(lines))


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.author.id != OWNER_ID:
        return
    if message.type not in USER_MESSAGE_TYPES:
        return  # ignore system messages (thread created, pins, joins, …)
    channel = message.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    if CHANNEL_ID and CHANNEL_ID not in (channel.id, getattr(channel, "parent_id", None)):
        return
    if message.content.startswith(bot.command_prefix):
        await bot.process_commands(message)
        return

    # One thread = one conversation
    if isinstance(channel, discord.TextChannel):
        title = " ".join(message.clean_content.split())[:80] or (
            message.attachments[0].filename if message.attachments else "Conversation"
        )
        thread = await message.create_thread(name=title, auto_archive_duration=1440)
    else:
        thread = channel

    try:
        async with thread.typing():
            history, atts = await build_history(thread)
            if is_save_command(message.content):
                await handle_save(thread, history, atts)
            elif is_edit_command(message.content):
                await handle_edit(thread, history)
            else:
                await handle_answer(thread, history)
    except errors.APIError as e:
        log.exception("Gemini error")
        if e.code == 429:
            await thread.send("⏳ Gemini rate limit, skús o chvíľu.")
        else:
            await thread.send(f"❌ Gemini error {e.code}: {str(e.message)[:300]}")
    except httpx.TimeoutException:
        log.exception("Gemini timeout")
        await thread.send("⌛ Gemini neodpovedal včas, skús znova.")
    except Exception as e:
        log.exception("Failed to handle message")
        await thread.send(f"❌ {type(e).__name__}: {str(e)[:300]}")


bot.run(DISCORD_TOKEN, root_logger=True)
