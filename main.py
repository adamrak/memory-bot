import asyncio
import base64
import io
import logging
import os
import re
import tarfile
import time
import unicodedata
from datetime import date
from urllib.parse import quote

import aiohttp
import discord
import httpx
from aiohttp import web
from discord.ext import commands, tasks
from google import genai
from google.genai import errors, types
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
GEMINI_MODELS = [m.strip() for m in os.getenv("GEMINI_MODEL", "gemini-3.8-flash,gemini-3.5-flash").split(",") if m.strip()]
GEMINI_THINKING = os.getenv("GEMINI_THINKING", "low").strip()  # "low" / "high" / empty = model default
GEMINI_TIMEOUT_S = int(os.getenv("GEMINI_TIMEOUT_S", "45"))
SYNC_MINUTES = float(os.getenv("SYNC_MINUTES", "5"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "50"))
HISTORY_MAX_CHARS = int(os.getenv("HISTORY_MAX_CHARS", "40000"))
PORT = os.getenv("PORT")  # set by Render -> starts keep-alive HTTP server

GITHUB_API = "https://api.github.com"
DISCORD_LIMIT = 2000
PREVIEW_CHARS = 1700
SAVE_TRIGGER = re.compile(r"^\s*(zapis|uloz|save)\b", re.IGNORECASE)
USER_MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)

ANSWER_PROMPT = """You are a personal memory assistant for a single user. Below are their notes \
from a private GitHub repository (markdown files).

Rules:
- Answer primarily from the notes. When you use a note, cite its path like (📁 path/to/file.md).
- If the notes do not contain the answer, say so clearly first. You may then add general knowledge, \
explicitly labelled as not coming from the notes.
- Never invent personal facts (models, dates, places, people, numbers) that are not in the notes.
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
- Start the entry with a heading line: "### {today} – <short title>".
- Write in the language of the conversation.

Path:
- If the user named a section/file (e.g. "do sekcie auto/stk"), use exactly that as the path \
and set path_from_user=true.
- Otherwise pick the best fitting existing file from the list below, or propose a new path \
"<topic>/<subject>.md", e.g. auto/stk.md, domacnost/pracka.md, zdravie/ockovanie.md \
(lowercase, kebab-case, no diacritics, reuse existing topic folders). Set path_from_user=false.

Existing files:
{tree}"""


class SavePlan(BaseModel):
    path: str
    path_from_user: bool
    content: str


# --- Helpers ---
def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def is_save_command(text: str) -> bool:
    return bool(SAVE_TRIGGER.match(strip_accents(text)))


def sanitize_path(raw: str) -> str:
    """Normalize to a path relative to MEMORY_DIR, e.g. 'auto/stk.md'."""
    parts = [p.strip() for p in raw.strip().strip("`").replace("\\", "/").split("/")]
    parts = [p for p in parts if p and p != "."]
    if not parts or any(p == ".." or p.startswith(".") for p in parts):
        raise ValueError(f"Invalid path: {raw!r}")
    path = "/".join(parts)
    if MEMORY_DIR and path.startswith(f"{MEMORY_DIR}/"):  # user typed "memory/auto/stk"
        path = path[len(MEMORY_DIR) + 1:]
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


def render_preview(plan: SavePlan, status: str = "💾 Save this?") -> str:
    body = plan.content.replace("```", "'''")
    if len(body) > PREVIEW_CHARS:
        body = body[:PREVIEW_CHARS] + "\n…"
    origin = "your path" if plan.path_from_user else "suggested"
    return f"{status}\n📁 `{plan.path}` ({origin})\n```md\n{body}\n```"


def _extract_markdown(tar_gz: bytes) -> dict[str, str]:
    """Returns {path relative to MEMORY_DIR: content} for every .md file under MEMORY_DIR."""
    prefix = f"{MEMORY_DIR}/" if MEMORY_DIR else ""
    files = {}
    with tarfile.open(fileobj=io.BytesIO(tar_gz), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile() or not member.name.lower().endswith(".md"):
                continue
            path = member.name.split("/", 1)[1]  # strip "<owner>-<repo>-<sha>/" prefix
            if not path.startswith(prefix):
                continue
            files[path[len(prefix):]] = tar.extractfile(member).read().decode("utf-8", errors="replace")
    return files


# --- GitHub-backed memory store ---
class MemoryStore:
    """In-memory copy of all .md files in the repo. GitHub stays the source of truth."""

    def __init__(self):
        self.files: dict[str, str] = {}
        self.sha: str | None = None
        self.branch = GITHUB_BRANCH
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def start(self):
        self._session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=aiohttp.ClientTimeout(total=60),
        )

    async def close(self):
        if self._session:
            await self._session.close()

    async def _ensure_branch(self):
        if not self.branch:
            async with self._session.get(f"{GITHUB_API}/repos/{GITHUB_REPO}") as r:
                r.raise_for_status()
                self.branch = (await r.json())["default_branch"]

    async def sync(self) -> bool:
        """Re-download the repo only if the branch head moved. Returns True if anything changed."""
        async with self._lock:
            await self._ensure_branch()
            async with self._session.get(
                f"{GITHUB_API}/repos/{GITHUB_REPO}/commits/{quote(self.branch)}",
                headers={"Accept": "application/vnd.github.sha"},
            ) as r:
                r.raise_for_status()
                head = (await r.text()).strip()
            if head == self.sha:
                return False
            async with self._session.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/tarball/{head}") as r:
                r.raise_for_status()
                data = await r.read()
            self.files = await asyncio.to_thread(_extract_markdown, data)
            self.sha = head
            log.info("Synced %d notes @ %s", len(self.files), head[:7])
            return True

    async def save(self, path: str, entry: str) -> tuple[str, bool]:
        """Append entry to path (create if missing). Returns (html_url, created)."""
        repo_path = f"{MEMORY_DIR}/{path}" if MEMORY_DIR else path
        url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{quote(repo_path)}"
        async with self._lock:
            await self._ensure_branch()
            for attempt in range(3):
                async with self._session.get(url, params={"ref": self.branch}) as r:
                    if r.status == 404:
                        existing, file_sha = None, None
                    else:
                        r.raise_for_status()
                        j = await r.json()
                        existing = base64.b64decode(j["content"]).decode("utf-8")
                        file_sha = j["sha"]

                content = f"{existing.rstrip()}\n\n{entry.strip()}\n" if existing else f"{entry.strip()}\n"
                body = {
                    "message": f"{'Create' if existing is None else 'Update'} {repo_path} via Discord memory bot",
                    "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                    "branch": self.branch,
                }
                if file_sha:
                    body["sha"] = file_sha

                async with self._session.put(url, json=body) as r:
                    if r.status == 409 and attempt < 2:  # file changed between read and write -> retry
                        continue
                    r.raise_for_status()
                    j = await r.json()
                # Next periodic sync picks up the new commit; update the cache now so answers see it immediately.
                self.files[path] = content
                return j["content"]["html_url"], existing is None
        raise RuntimeError("GitHub write conflict, try again")


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


async def gemini_answer(history: list[types.Content]) -> str:
    notes = "\n".join(f"\n--- FILE: {p} ---\n{t}" for p, t in sorted(store.files.items())) or "(no notes yet)"
    system = ANSWER_PROMPT.format(count=len(store.files), notes=notes, today=date.today())
    log.info("Answering: %d history turns, %d chars of notes", len(history), len(notes))
    resp = await generate(history, make_config(system_instruction=system))
    return resp.text or "⚠️ Gemini returned an empty response (possibly blocked by safety filters)."


async def gemini_save_plan(history: list[types.Content]) -> SavePlan:
    tree = "\n".join(sorted(store.files)) or "(empty repository)"
    resp = await generate(
        history,
        make_config(
            system_instruction=SAVE_PROMPT.format(today=date.today(), tree=tree),
            response_mime_type="application/json",
            response_schema=SavePlan,
        ),
    )
    plan = resp.parsed if isinstance(resp.parsed, SavePlan) else SavePlan.model_validate_json(resp.text or "")
    try:
        plan.path = sanitize_path(plan.path)
    except ValueError:
        plan.path, plan.path_from_user = "inbox.md", False
    return plan


# --- Discord ---
async def build_history(thread: discord.Thread) -> list[types.Content]:
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

    msgs = [m for m in msgs if m.type in USER_MESSAGE_TYPES and m.clean_content]

    # Keep the newest messages within the char budget
    kept, total = [], 0
    for m in reversed(msgs):
        total += len(m.clean_content)
        if total > HISTORY_MAX_CHARS and kept:
            break
        kept.append(m)
    kept.reverse()

    contents: list[types.Content] = []
    for m in kept:
        role = "model" if m.author == bot.user else "user"
        part = types.Part.from_text(text=m.clean_content)
        if contents and contents[-1].role == role:
            contents[-1].parts.append(part)
        else:
            contents.append(types.Content(role=role, parts=[part]))
    while contents and contents[0].role == "model":
        contents.pop(0)
    return contents


class SaveView(discord.ui.View):
    def __init__(self, plan: SavePlan):
        super().__init__(timeout=900)
        self.plan = plan
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == OWNER_ID

    @discord.ui.button(label="Save", emoji="✅", style=discord.ButtonStyle.success)
    async def save(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(content=render_preview(self.plan, "⏳ Saving…"), view=None)
        try:
            url, created = await store.save(self.plan.path, self.plan.content)
        except Exception as e:
            log.exception("Save failed")
            status = f"❌ Save failed: {str(e)[:300]}"
            await interaction.edit_original_response(content=render_preview(self.plan, status), view=self)
            return
        self.stop()
        status = f"✅ {'Created' if created else 'Updated'} [{self.plan.path}](<{url}>)"
        await interaction.edit_original_response(content=render_preview(self.plan, status), view=None)

    @discord.ui.button(label="Change path", emoji="📝", style=discord.ButtonStyle.secondary)
    async def change_path(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_modal(PathModal(self))

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="❌ Cancelled, nothing saved.", view=None)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content=render_preview(self.plan, "⌛ Expired, nothing saved."), view=None)
            except discord.HTTPException:
                pass


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
        await interaction.response.edit_message(content=render_preview(self.save_view.plan), view=self.save_view)


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
    await ctx.reply(f"{state}: {len(store.files)} notes @ `{(store.sha or '-')[:7]}`")


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
        title = " ".join(message.clean_content.split())[:80] or "Conversation"
        thread = await message.create_thread(name=title, auto_archive_duration=1440)
    else:
        thread = channel

    try:
        async with thread.typing():
            history = await build_history(thread)
            if is_save_command(message.content):
                plan = await gemini_save_plan(history)
                view = SaveView(plan)
                view.message = await thread.send(render_preview(plan), view=view)
            else:
                for chunk in split_message(await gemini_answer(history)):
                    await thread.send(chunk)
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
