"""Gemini calls: answering questions and planning saves/edits."""
import logging
import time
from datetime import date

import httpx
from google import genai
from google.genai import errors, types

from .config import GEMINI_API_KEY, GEMINI_MEDIA_TIMEOUT_S, GEMINI_MODELS, GEMINI_THINKING, GEMINI_TIMEOUT_S
from .models import EditPlan, SavePlan
from .paths import note_path

log = logging.getLogger("memorybot")

ANSWER_PROMPT = """You are a personal memory assistant for a single user. Below are their notes \
from a private GitHub repository (markdown files).

Rules:
- Answer primarily from the notes. When you use a note, cite its path like (📁 path/to/file.md).
- If the notes do not contain the answer, say so clearly first. You may then add general knowledge, \
explicitly labelled as not coming from the notes.
- Never invent personal facts (models, dates, places, people, numbers) that are not in the notes.
- Images/PDFs the user sent in this conversation are included inline; use them.
- Attachments marked "not read" were not shown to you. If the question needs one, tell the user \
to ask you to read it (e.g. "prečítaj ten PDF").
- Notes may link stored files (lines starting with 📎). If the user asks to see a stored file, or a stored \
photo is essential for the answer (e.g. where something is), add at the very end one line per file: \
ATTACH: <file path exactly as linked in the note> (max 3). The bot sends those files. Never mention this mechanism.
- You cannot save, change, move, rename or delete anything. Never claim or imitate that something was \
saved, changed or committed (no "✅ Done", diffs or commit links). If the user asks for a change, tell them \
to start the message with a command word: "zapíš …" to save, "oprav / premenuj / presuň / zmaž …" to edit, \
"vráť / undo" to revert the last change.
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
- Otherwise choose the location yourself and set path_from_user=false. Paths can be nested as deep \
as useful: <topic>/<subtopic>/…/<name>.md, e.g. auto/stk.md, domacnost/pracka.md, \
elektronika/akaso-brave/akaso-brave.md.
- Give a specific item (device, car, appliance, contract…) its own folder when it has or will likely \
get several files (manual, receipt, photos, notes). Name the main note after the item.
- Prefer existing folders and notes. Don't create a new level for a single small fact.
- Names: lowercase, kebab-case, ASCII only, no diacritics, spaces or brackets.

Attachments:
- Attachments in this conversation are listed below (ref: filename, type, size).
- If the user wants to save some of them ("zapíš si to" right after a photo, "tú fotku", "všetky účtenky"), \
list them with a short descriptive kebab-case name without extension or date, e.g. zarucak-pracka. \
Otherwise return an empty list.
- Attachments marked "not read" were not shown to you: base the entry only on what the user said \
and the filename, never guess their contents.

Existing files:
{tree}

Attachments in this conversation:
{attachments}"""

EDIT_PROMPT = """The user wants to edit, move or delete something in their notes. All notes are below.

Return the minimal set of file changes:
- action "update": path + the COMPLETE new content of the file. Keep unchanged parts verbatim.
- action "delete": path, when the whole file should be removed (new_content empty).
- To move content, update/delete the source file and update/create the target file.
- Do not touch files that don't need to change.
- Copy attachment links (lines starting with 📎) byte-for-byte, also when moving or renaming a note \
or folder. Never change the path inside a 📎 link; the bot moves the files next to the note and fixes \
the links itself. Remove a 📎 line only when removing the fact it belongs to.
- delete_attachments: paths (as linked) of files the user explicitly wants deleted, or that belong to \
a note/fact the user explicitly asked to delete. Moving, renaming or editing never deletes files. \
Otherwise return an empty list.
- summary: one short sentence describing the change, in the user's language.
- If the request is unclear or nothing matches, return no changes and explain why in summary.
- Paths are relative, e.g. domacnost/pracka.md.

=== NOTES ===
{notes}
=== END OF NOTES ===

Today is {today}."""

EMPTY_ANSWER = "⚠️ Gemini returned an empty response (possibly blocked by safety filters)."

_client: genai.Client | None = None


def client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_S * 1000))
    return _client


def make_config(**kwargs) -> types.GenerateContentConfig:
    if GEMINI_THINKING:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=GEMINI_THINKING)
    return types.GenerateContentConfig(
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        **kwargs,
    )


async def generate(contents: list[types.Content], config: types.GenerateContentConfig) -> types.GenerateContentResponse:
    """Try models in order; fall back to the next one on server errors / timeouts."""
    has_media = any(p.inline_data for c in contents for p in (c.parts or []))
    timeout_s = GEMINI_MEDIA_TIMEOUT_S if has_media else GEMINI_TIMEOUT_S
    config.http_options = types.HttpOptions(timeout=timeout_s * 1000)
    last_error: Exception | None = None
    for model in GEMINI_MODELS:
        start = time.monotonic()
        try:
            resp = await client().aio.models.generate_content(model=model, contents=contents, config=config)
            log.info("Gemini %s answered in %.1fs (media: %s)", model, time.monotonic() - start, has_media)
            return resp
        except (errors.ServerError, httpx.TimeoutException) as e:
            log.warning("Gemini %s failed after %.1fs (media: %s): %s", model, time.monotonic() - start, has_media, e)
            last_error = e
    raise last_error


def parse_json(resp: types.GenerateContentResponse, model_cls):
    return resp.parsed if isinstance(resp.parsed, model_cls) else model_cls.model_validate_json(resp.text or "")


def notes_text(notes: dict[str, str]) -> str:
    return "\n".join(f"\n--- FILE: {p} ---\n{t}" for p, t in sorted(notes.items())) or "(no notes yet)"


async def answer(history: list[types.Content], notes: dict[str, str]) -> str:
    system = ANSWER_PROMPT.format(count=len(notes), notes=notes_text(notes), today=date.today())
    resp = await generate(history, make_config(system_instruction=system))
    return resp.text or EMPTY_ANSWER


async def save_plan(
    history: list[types.Content], notes: dict[str, str], attachments: dict, thread_attachments: list[str]
) -> SavePlan:
    system = SAVE_PROMPT.format(
        today=date.today(),
        tree="\n".join(sorted([*notes, *attachments])) or "(empty)",
        attachments="\n".join(thread_attachments) or "(none)",
    )
    resp = await generate(
        history,
        make_config(system_instruction=system, response_mime_type="application/json", response_schema=SavePlan),
    )
    plan = parse_json(resp, SavePlan)
    try:
        plan.path = note_path(plan.path, notes)
    except ValueError:
        plan.path, plan.path_from_user = "inbox.md", False
    return plan


async def edit_plan(history: list[types.Content], notes: dict[str, str]) -> EditPlan:
    system = EDIT_PROMPT.format(notes=notes_text(notes), today=date.today())
    resp = await generate(
        history,
        make_config(system_instruction=system, response_mime_type="application/json", response_schema=EditPlan),
    )
    return parse_json(resp, EditPlan)


async def ping(model: str) -> str:
    """Bare one-word request (no notes, no history), used by !ping."""
    resp = await client().aio.models.generate_content(model=model, contents="Reply with one word: hi", config=make_config())
    return (resp.text or "").strip()
