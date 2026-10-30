from datetime import date

import pytest

from memorybot.paths import (
    folder_of, link_target, note_path, planned_attachment_path, refs_in, relative_path, rewrite_links, sanitize_path,
    slugify,
)


@pytest.mark.parametrize("raw, expected", [
    ("auto/stk", "auto/stk.md"),
    ("auto/stk.md", "auto/stk.md"),
    ("/memory/auto/STK.md", "auto/stk.md"),
    ("`domacnost/pracka`", "domacnost/pracka.md"),
    ("Elektronika/Akaso (GoPro)/Návod", "elektronika/akaso-gopro/navod.md"),
    ("zdravie\\očkovanie", "zdravie/ockovanie.md"),
    ("./auto//stk", "auto/stk.md"),
])
def test_sanitize_path(raw, expected):
    assert sanitize_path(raw) == expected


@pytest.mark.parametrize("raw", ["", "/", "..", "a/../b", "auto/(())"])
def test_sanitize_path_rejects_invalid(raw):
    with pytest.raises(ValueError):
        sanitize_path(raw)


def test_note_path_keeps_existing_names_and_normalizes_new_ones():
    notes = {"Auto/STK poznámky.md": "x"}
    assert note_path("Auto/STK poznámky.md", notes) == "Auto/STK poznámky.md"
    assert note_path("/memory/Auto/STK poznámky.md", notes) == "Auto/STK poznámky.md"
    assert note_path("Auto/Nové", notes) == "auto/nove.md"


def test_slugify():
    assert slugify("Záručný list – Pračka Bosch!") == "zarucny-list-pracka-bosch"
    assert slugify("a" * 100) == "a" * 60


def test_relative_path():
    assert relative_path(" /memory/auto/stk.md ") == "auto/stk.md"
    assert relative_path("memoryfoo/x.md") == "memoryfoo/x.md"


def test_folder_of():
    assert folder_of("a/b/c.md") == "a/b/"
    assert folder_of("c.md") == ""


def test_planned_attachment_path_is_next_to_note():
    day = date(2026, 10, 30)
    assert planned_attachment_path("elektronika/akaso/akaso.md", "Návod k Akaso", ".pdf", day) == \
        "elektronika/akaso/2026-10-30-navod-k-akaso.pdf"
    assert planned_attachment_path("inbox.md", "", ".jpg", day) == "2026-10-30-file.jpg"


def test_refs_in_finds_only_paperclip_links():
    text = (
        "📎 [a.pdf](/memory/attachments/elektronika/a.pdf)\n"
        "📎[b.jpg](/memory/auto/b.jpg)\n"
        "[normal link](https://example.com)\n"
        "[c.pdf](/memory/c.pdf)\n"
    )
    assert refs_in([text]) == {"attachments/elektronika/a.pdf", "auto/b.jpg"}


def test_rewrite_links_only_touches_moved_files():
    text = "📎 [a.pdf](/memory/x/a.pdf)\n📎 [b.pdf](/memory/x/b.pdf)\n"
    out = rewrite_links(text, {"x/a.pdf": "y/a.pdf"})
    assert out == f"📎 [a.pdf]({link_target('y/a.pdf')})\n📎 [b.pdf](/memory/x/b.pdf)\n"
