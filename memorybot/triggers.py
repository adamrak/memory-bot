"""Command words at the start of a message (matched without diacritics)."""
import re

from .paths import strip_accents

SAVE_TRIGGER = re.compile(
    r"^\s*(zapis|zapisat|zapisu|uloz|ulozit|dopln|doplnit|pridaj|pridat|save|add)\b", re.IGNORECASE
)
EDIT_TRIGGER = re.compile(
    r"^\s*(oprav|opravit|uprav|upravit|zmen|zmenit|aktualizuj|aktualizovat|prepis|prepisat|zmaz|zmazat|vymaz|vymazat"
    r"|presun|presunut|premenuj|premenovat|edit|update|delete|remove|move|rename)\b",
    re.IGNORECASE,
)
REVERT_TRIGGER = re.compile(r"^\s*(vrat|vratit|revert|undo)\b", re.IGNORECASE)
PDF_READ_TRIGGER = re.compile(  # anywhere in the message
    r"\b(precitaj|precitat|analyzuj|rozober|parsuj|vytiahni|read|parse|extract|analy[sz]e)\b", re.IGNORECASE
)


def is_save_command(text: str) -> bool:
    return bool(SAVE_TRIGGER.match(strip_accents(text)))


def is_edit_command(text: str) -> bool:
    return bool(EDIT_TRIGGER.match(strip_accents(text)))


def is_revert_command(text: str) -> bool:
    return bool(REVERT_TRIGGER.match(strip_accents(text)))


def wants_pdf_read(text: str) -> bool:
    return bool(PDF_READ_TRIGGER.search(strip_accents(text)))
