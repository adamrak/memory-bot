"""Discord previews with confirm buttons. Nothing is committed until the owner clicks."""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import discord

from .changes import (
    BlobRef, Changes, ConflictError, PreparedFile, build_edit_changes, build_save_changes, prepare_edit, render_diff,
    revert_changes,
)
from .config import DISCORD_LIMIT, EDIT_DIFF_CHARS, MAX_ATTACHMENT_BYTES, MAX_ATTACHMENT_MB, OWNER_ID
from .files import prepare_file
from .history import ThreadAttachment
from .models import EditPlan, SavePlan
from .paths import note_path, planned_attachment_path
from .store import MemoryStore

log = logging.getLogger("memorybot")


class OwnerView(discord.ui.View):
    def __init__(self, store: MemoryStore):
        super().__init__(timeout=900)
        self.store = store
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


# --- Save ---
async def commit_save(store: MemoryStore, plan: SavePlan, picks: list[tuple[ThreadAttachment, str]]) -> str:
    files = []
    for t, name in picks:
        if t.attachment.size > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"{t.attachment.filename} is larger than {MAX_ATTACHMENT_MB} MB")
        data, ext = await asyncio.to_thread(prepare_file, await t.attachment.read(), t.attachment.filename, t.mime)
        files.append(PreparedFile(name, ext, data))
    return await store.apply(
        lambda: build_save_changes(plan, files, store.files, store.attachments),
        f"Save {store.repo_path(plan.path)} via Discord memory bot",
    )


class SaveView(OwnerView):
    def __init__(self, store: MemoryStore, plan: SavePlan, picks: list[tuple[ThreadAttachment, str]]):
        super().__init__(store)
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
        await self.run_commit(interaction, "⏳ Saving…", lambda: commit_save(self.store, self.plan, picks))

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
        self.path_input = discord.ui.TextInput(label="Path (e.g. auto/stk)", default=save_view.plan.path, max_length=200)
        self.add_item(self.path_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            path = note_path(self.path_input.value, self.save_view.store.files)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        self.save_view.plan.path = path
        self.save_view.plan.path_from_user = True
        await interaction.response.edit_message(content=self.save_view.render(), view=self.save_view)


# --- Edit ---
class EditView(OwnerView):
    def __init__(self, store: MemoryStore, plan: EditPlan):
        super().__init__(store)
        self.plan = plan
        self.prep = prepare_edit(plan, store.files, store.attachments)
        self.diff = render_diff(self.prep.changes, self.prep.base)

    @property
    def diff_truncated(self) -> bool:
        return len(self.diff) > EDIT_DIFF_CHARS

    def render(self, status: str | None = None) -> str:
        lines = [status or "✏️ Apply these changes?", self.plan.summary[:300]]
        lines += _capped([f"📦 `{old}` → `{new}`" for old, new in self.prep.moves.items()], "attachments moved")
        lines += _capped([f"🗑 `{p}` (attachment deleted)" for p in self.prep.deletions], "attachments deleted")
        lines += _capped(
            [f"⚠️ `{p}` is no longer linked from any note (file kept)" for p in self.prep.unlinked], "unlinked files kept"
        )
        diff = self.diff.replace("```", "'''")
        if self.diff_truncated:
            diff = diff[:EDIT_DIFF_CHARS] + "\n… (full diff in changes.diff)"
        return "\n".join(lines) + f"\n```diff\n{diff}\n```"

    def build(self) -> Changes:
        return build_edit_changes(self.prep, self.store.files, self.store.attachments)

    @discord.ui.button(label="Apply", emoji="✅", style=discord.ButtonStyle.success)
    async def apply(self, interaction: discord.Interaction, _button: discord.ui.Button):
        message = f"Edit via Discord memory bot: {self.plan.summary}"[:250]
        await self.run_commit(interaction, "⏳ Applying…", lambda: self.store.apply(self.build, message))

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cancel_action(interaction)


def _capped(lines: list[str], what: str, limit: int = 5) -> list[str]:
    if len(lines) <= limit:
        return lines
    return lines[:limit] + [f"… and {len(lines) - limit} more {what}"]


# --- Revert ---
@dataclass
class RevertPlan:
    commit: dict
    changes: Changes
    skipped: list[str]  # files outside MEMORY_DIR, not reverted


async def plan_revert(store: MemoryStore) -> RevertPlan:
    """Undo the latest commit: restore every file it touched to its parent's version."""
    commit = await store.head_commit()
    if len(commit["parents"]) != 1:
        raise ValueError("The last commit is the first commit or a merge; it can't be reverted here.")
    parent = await store.tree_blobs(commit["parents"][0]["sha"])
    changes, skipped = revert_changes(commit, parent, store.memory_dir)
    return RevertPlan(commit, changes, skipped)


class RevertView(OwnerView):
    def __init__(self, store: MemoryStore, plan: RevertPlan):
        super().__init__(store)
        self.plan = plan

    @property
    def title(self) -> str:
        return self.plan.commit["commit"]["message"].splitlines()[0]

    def render(self, status: str | None = None) -> str:
        c = self.plan.commit
        lines = [
            status or "↩️ Revert the last commit?",
            f"`{c['sha'][:7]}` {self.title[:200]}",
            f"{c['commit']['author']['name']}, {c['commit']['author']['date'][:16].replace('T', ' ')} UTC",
        ]
        lines += _capped(
            [f"♻️ `{p}` restored" if isinstance(v, BlobRef) else f"🗑 `{p}` removed" for p, v in sorted(self.plan.changes.items())],
            "files", limit=15,
        )
        lines += [f"⚠️ `{p}` is outside `{self.store.memory_dir}/`, not reverted" for p in self.plan.skipped[:5]]
        return "\n".join(lines)

    def build(self) -> Changes:
        if self.store.sha != self.plan.commit["sha"]:
            raise ConflictError("There are newer commits since this preview, ask again.")
        return dict(self.plan.changes)

    @discord.ui.button(label="Revert", emoji="↩️", style=discord.ButtonStyle.danger)
    async def revert(self, interaction: discord.Interaction, _button: discord.ui.Button):
        message = f'Revert "{self.title[:150]}" via Discord memory bot\n\nThis reverts commit {self.plan.commit["sha"]}.'
        await self.run_commit(interaction, "⏳ Reverting…", lambda: self.store.apply(self.build, message))

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cancel_action(interaction)
