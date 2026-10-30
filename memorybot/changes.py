"""Planning of repository changes for save, edit and revert.

Everything here is pure (no network, no Discord): it takes the current notes/attachments and returns
a change set {path: value} that MemoryStore.apply() commits atomically. Values:
  str      -> new text content
  bytes    -> new binary content
  BlobRef  -> reuse an existing blob (moves/restores without downloading)
  None     -> delete the file
"""
import difflib
import os
from dataclasses import dataclass
from datetime import date

from .config import MEMORY_DIR
from .models import EditPlan, SavePlan
from .paths import basename, folder_of, link_target, note_path, planned_attachment_path, refs_in, relative_path, rewrite_links


class ConflictError(Exception):
    """The repository changed since the preview was shown."""


@dataclass(frozen=True)
class BlobRef:
    """Reuse an existing blob, so attachments can be moved or restored without downloading them."""
    sha: str


Value = str | bytes | BlobRef | None
Changes = dict[str, Value]
Notes = dict[str, str]  # note path -> content
Attachments = dict[str, tuple[str, int]]  # attachment path -> (blob sha, size)


# --- Save ---
@dataclass
class PreparedFile:
    name: str  # descriptive kebab-case name chosen by Gemini
    ext: str
    data: bytes


def build_save_changes(
    plan: SavePlan, files: list[PreparedFile], notes: Notes, attachments: Attachments, today: date | None = None
) -> Changes:
    """Append the entry to the note and store files next to it, linked with 📎 lines."""
    changes: Changes = {}
    links = []
    for f in files:
        base = planned_attachment_path(plan.path, f.name, "", today)
        path, i = f"{base}{f.ext}", 2
        while path in attachments or path in changes:
            path, i = f"{base}-{i}{f.ext}", i + 1
        changes[path] = f.data
        links.append(f"📎 [{basename(path)}]({link_target(path)})")
    entry = plan.content.strip()
    if links:
        entry += "\n\n" + "\n".join(links)
    existing = notes.get(plan.path)
    changes[plan.path] = f"{existing.rstrip()}\n\n{entry}\n" if existing else f"{entry}\n"
    return changes


# --- Edit ---
@dataclass
class EditPrep:
    changes: dict[str, str | None]  # note path -> new content / None = delete
    base: dict[str, str | None]  # note contents the preview was computed from
    deletions: list[str]  # attachments to delete (explicitly requested and no longer linked)
    unlinked: list[str]  # attachments no longer linked but kept
    moves: dict[str, str]  # attachment old path -> new path


def repair_links(changes: dict[str, str | None], base: dict[str, str | None], attachments: Attachments) -> dict[str, str]:
    """Gemini sometimes rewrites 📎 links itself when moving notes, pointing to files that don't exist yet.
    Map such links back to the real file (same file name, linked from one of the edited notes)."""
    by_name: dict[str, list[str]] = {}
    for ref in refs_in(v for v in base.values() if v) & attachments.keys():
        by_name.setdefault(basename(ref), []).append(ref)
    fixes: dict[str, str] = {}
    for text in changes.values():
        for ref in refs_in([text] if text else []):
            candidates = by_name.get(basename(ref), [])
            if ref not in attachments and len(candidates) == 1:
                fixes[ref] = candidates[0]
    return fixes


def plan_attachment_moves(changes: dict[str, str | None], notes: Notes, attachments: Attachments) -> dict[str, str]:
    """Attachments linked only from a changed note, but living in another folder, move next to that note."""
    after = {**notes, **changes}
    owners: dict[str, set[str]] = {}
    for note, text in after.items():
        if text is not None:
            for ref in refs_in([text]):
                owners.setdefault(ref, set()).add(note)
    moves: dict[str, str] = {}
    taken = set(attachments)
    for note, text in changes.items():
        if text is None:
            continue
        folder = folder_of(note)
        for ref in sorted(refs_in([text])):
            if ref not in attachments or ref in moves or owners.get(ref) != {note} or folder_of(ref) == folder:
                continue  # unknown, shared with another note, or already in place
            stem, ext = os.path.splitext(basename(ref))
            target, i = f"{folder}{stem}{ext}", 2
            while target in taken:
                target, i = f"{folder}{stem}-{i}{ext}", i + 1
            taken.add(target)
            moves[ref] = target
    return moves


def _rewrite_all(changes: dict[str, str | None], moves: dict[str, str]) -> dict[str, str | None]:
    if not moves:
        return changes
    return {p: rewrite_links(v, moves) if v is not None else None for p, v in changes.items()}


def prepare_edit(plan: EditPlan, notes: Notes, attachments: Attachments) -> EditPrep:
    """Turn Gemini's edit plan into a safe change preview.
    Attachments follow their notes and are deleted only when Gemini says the user explicitly asked for it."""
    changes: dict[str, str | None] = {}
    base: dict[str, str | None] = {}
    for c in plan.changes:
        ext = os.path.splitext(c.path.strip())[1].lower()
        if ext and ext != ".md":
            continue  # attachments are handled by the bot, not by Gemini
        try:
            path = note_path(c.path, notes)
        except ValueError:
            continue
        old = notes.get(path)
        new = None if c.action == "delete" else c.new_content.rstrip() + "\n"
        if (old is None and new is None) or (old is not None and new is not None and old.rstrip() == new.rstrip()):
            continue
        changes[path], base[path] = new, old

    changes = _rewrite_all(changes, repair_links(changes, base, attachments))
    moves = plan_attachment_moves(changes, notes, attachments)
    changes = _rewrite_all(changes, moves)

    linked_after = refs_in(v for v in {**notes, **changes}.values() if v is not None)
    unlinked = (refs_in(notes.values()) - linked_after - moves.keys()) & attachments.keys()
    requested = {relative_path(p) for p in plan.delete_attachments}
    return EditPrep(changes, base, sorted(unlinked & requested), sorted(unlinked - requested), moves)


def build_edit_changes(prep: EditPrep, notes: Notes, attachments: Attachments) -> Changes:
    """Final change set at commit time; refuses if notes or attachments changed since the preview."""
    for path, old in prep.base.items():
        if notes.get(path) != old:
            raise ConflictError(f"`{path}` changed since this preview, ask again.")
    for old, new in prep.moves.items():
        if old not in attachments or new in attachments:
            raise ConflictError(f"`{old}` changed since this preview, ask again.")
    changes: Changes = dict(prep.changes)
    for old, new in prep.moves.items():
        changes[new] = BlobRef(attachments[old][0])
        changes[old] = None
    linked = refs_in(v for v in {**notes, **prep.changes}.values() if v is not None)
    for path in prep.deletions:
        if path in attachments and path not in linked:
            changes[path] = None
    return changes


def render_diff(changes: dict[str, str | None], base: dict[str, str | None]) -> str:
    out: list[str] = []
    for path, new in changes.items():
        old = base[path]
        out.extend(difflib.unified_diff(
            (old or "").splitlines(), (new or "").splitlines(),
            f"a/{path}" if old is not None else "/dev/null",
            f"b/{path}" if new is not None else "/dev/null",
            lineterm="", n=1,
        ))
    return "\n".join(out)


# --- Revert ---
def revert_changes(commit: dict, parent_blobs: dict[str, str], memory_dir: str = MEMORY_DIR) -> tuple[Changes, list[str]]:
    """Restore every file a commit touched to its parent's version.
    `commit` is GitHub's GET /commits/{sha} response, `parent_blobs` maps repo path -> blob sha in the parent.
    Returns (changes relative to memory_dir, repo paths outside memory_dir that are skipped)."""
    prefix = f"{memory_dir}/" if memory_dir else ""
    changes: Changes = {}
    skipped: list[str] = []
    for f in commit["files"]:
        name, status = f["filename"], f["status"]
        if status == "renamed":
            targets = [(name, None), (f["previous_filename"], parent_blobs.get(f["previous_filename"]))]
        elif status in ("added", "copied"):
            targets = [(name, None)]
        else:  # modified, removed, changed
            targets = [(name, parent_blobs.get(name))]
        for path, sha in targets:
            if not path.startswith(prefix):
                skipped.append(path)
                continue
            changes[path[len(prefix):]] = BlobRef(sha) if sha else None
    return changes, skipped
