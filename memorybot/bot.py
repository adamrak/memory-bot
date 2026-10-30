"""Discord bot: message routing, commands, keep-alive server and periodic sync."""
import io
import logging
import time

import discord
import httpx
from aiohttp import web
from discord.ext import commands, tasks
from google.genai import errors, types

from . import config, gemini
from .history import USER_MESSAGE_TYPES, ThreadAttachment, build_history
from .paths import basename
from .ratelimit import format_rate_limit, parse_rate_limit
from .store import MemoryStore
from .text import extract_attach_lines, split_message
from .triggers import is_edit_command, is_revert_command, is_save_command, wants_pdf_read
from .ui import EditView, RevertView, SaveView, plan_revert

log = logging.getLogger("memorybot")

store = MemoryStore(config.GITHUB_REPO, config.GITHUB_TOKEN, config.GITHUB_BRANCH)


# --- Handlers ---
async def send_stored_files(channel: discord.abc.Messageable, paths: list[str]):
    files, links = [], []
    for path in paths:
        sha, size = store.attachments[path]
        if size > config.DISCORD_FILE_LIMIT:
            links.append(f"📎 [{basename(path)}](<{store.blob_url(path)}>)")
        else:
            files.append(discord.File(io.BytesIO(await store.read_blob(sha)), filename=basename(path)))
    kwargs = {}
    if links:
        kwargs["content"] = "\n".join(links)
    if files:
        kwargs["files"] = files
    if kwargs:
        await channel.send(**kwargs)


async def handle_answer(thread: discord.Thread, history: list[types.Content]):
    text, paths = extract_attach_lines(await gemini.answer(history, store.files), store.attachments)
    for chunk in split_message(text):
        await thread.send(chunk)
    if paths:
        await send_stored_files(thread, paths)


async def handle_save(thread: discord.Thread, history: list[types.Content], atts: list[ThreadAttachment]):
    plan = await gemini.save_plan(history, store.files, store.attachments, [t.describe() for t in atts])
    by_ref = {t.ref: t for t in atts}
    picks, seen = [], set()
    for pick in plan.attachments:
        if pick.ref in by_ref and pick.ref not in seen:
            seen.add(pick.ref)
            picks.append((by_ref[pick.ref], pick.name))
    view = SaveView(store, plan, picks[:25])
    view.message = await thread.send(view.render(), view=view)


async def handle_edit(thread: discord.Thread, history: list[types.Content]):
    plan = await gemini.edit_plan(history, store.files)
    view = EditView(store, plan)
    if not view.prep.changes:
        await thread.send(f"🤷 {plan.summary or 'Nothing to change.'}")
        return
    kwargs = {}
    if view.diff_truncated:
        kwargs["file"] = discord.File(io.BytesIO(view.diff.encode("utf-8")), filename="changes.diff")
    view.message = await thread.send(view.render(), view=view, **kwargs)


async def handle_revert(channel: discord.abc.Messageable):
    plan = await plan_revert(store)
    if not plan.changes:
        await channel.send(f"🤷 The last commit `{plan.commit['sha'][:7]}` changed nothing in `{store.memory_dir}/`.")
        return
    view = RevertView(store, plan)
    view.message = await channel.send(view.render(), view=view)


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


@tasks.loop(minutes=config.SYNC_MINUTES)
async def periodic_sync():
    try:
        await store.sync()
    except Exception:
        log.exception("Periodic sync failed")


class MemoryBot(commands.Bot):
    async def setup_hook(self):
        await store.start()
        if config.PORT:
            await start_health_server(int(config.PORT))
        periodic_sync.start()  # first iteration runs immediately = initial sync

    async def close(self):
        periodic_sync.cancel()
        await store.close()
        await super().close()


intents = discord.Intents.default()
intents.message_content = True
bot = MemoryBot(command_prefix="!", intents=intents, owner_id=config.OWNER_ID or None)


@bot.event
async def on_ready():
    log.info("Logged in as %s (models: %s, thinking: %s)",
             bot.user, ", ".join(config.GEMINI_MODELS), config.GEMINI_THINKING or "default")


@bot.command(name="sync")
async def sync_cmd(ctx: commands.Context):
    try:
        changed = await store.sync()
    except Exception as e:
        await ctx.reply(f"❌ Sync failed: {e}")
        return
    state = "🔄 Synced" if changed else "✅ Already up to date"
    await ctx.reply(f"{state}: {len(store.files)} notes, {len(store.attachments)} attachments @ `{(store.sha or '-')[:7]}`")


@bot.command(name="undo", aliases=["revert"])
async def undo_cmd(ctx: commands.Context):
    try:
        await handle_revert(ctx.channel)
    except Exception as e:
        log.exception("Revert failed")
        await ctx.reply(f"❌ {type(e).__name__}: {str(e)[:300]}")


@bot.command(name="ping")
async def ping_cmd(ctx: commands.Context):
    """Bare Gemini call (no notes, no history) against each configured model, with timing."""
    lines = []
    async with ctx.typing():
        for model in config.GEMINI_MODELS:
            start = time.monotonic()
            try:
                reply = await gemini.ping(model)
                lines.append(f"✅ `{model}` {time.monotonic() - start:.1f}s → {reply[:50]!r}")
            except Exception as e:
                lines.append(f"❌ `{model}` {time.monotonic() - start:.1f}s → {type(e).__name__}: {str(e)[:150]}")
    await ctx.reply("\n".join(lines))


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.author.id != config.OWNER_ID:
        return
    if message.type not in USER_MESSAGE_TYPES:
        return  # ignore system messages (thread created, pins, joins, …)
    channel = message.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    if config.CHANNEL_ID and config.CHANNEL_ID not in (channel.id, getattr(channel, "parent_id", None)):
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
            if is_revert_command(message.content):
                await handle_revert(thread)
                return
            history, atts = await build_history(thread, bot.user.id, include_pdfs=wants_pdf_read(message.content))
            if is_save_command(message.content):
                await handle_save(thread, history, atts)
            elif is_edit_command(message.content):
                await handle_edit(thread, history)
            else:
                await handle_answer(thread, history)
    except errors.APIError as e:
        log.exception("Gemini error")
        if e.code == 429:
            limit = parse_rate_limit(e.details)
            log.warning("Gemini rate limit: quota=%s retry_at=%s", limit.quota_id, limit.retry_at)
            await thread.send(format_rate_limit(limit))
        else:
            await thread.send(f"❌ Gemini error {e.code}: {str(e.message)[:300]}")
    except httpx.TimeoutException:
        log.exception("Gemini timeout")
        await thread.send("⌛ Gemini neodpovedal včas, skús znova.")
    except Exception as e:
        log.exception("Failed to handle message")
        await thread.send(f"❌ {type(e).__name__}: {str(e)[:300]}")


def run():
    config.validate()
    bot.run(config.DISCORD_TOKEN, root_logger=True)
