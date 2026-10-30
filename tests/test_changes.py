from datetime import date

import pytest

from memorybot.changes import (
    BlobRef, ConflictError, PreparedFile, build_edit_changes, build_save_changes, prepare_edit, render_diff,
    revert_changes,
)
from memorybot.models import EditPlan, FileChange, SavePlan

DAY = date(2026, 10, 30)
PDF = "elektronika/akaso-brave/2026-10-30-navod.pdf"
NOTE = f"### Akaso\nmanual\n\n📎 [2026-10-30-navod.pdf](/memory/{PDF})\n"


def notes_and_attachments():
    notes = {
        "elektronika/akaso-brave/index.md": NOTE,
        "auto/stk.md": "stk\n📎 [s.jpg](/memory/auto/s.jpg)\n",
        "a.md": "📎 [sh.pdf](/memory/shared/sh.pdf)\n",
        "b/b.md": "📎 [sh.pdf](/memory/shared/sh.pdf)\n",
    }
    attachments = {PDF: ("sha-pdf", 10), "auto/s.jpg": ("sha-jpg", 5), "shared/sh.pdf": ("sha-sh", 1)}
    return notes, attachments


def edit(*changes, delete=()):
    return EditPlan(summary="test", changes=list(changes), delete_attachments=list(delete))


def update(path, content):
    return FileChange(path=path, action="update", new_content=content)


def delete(path):
    return FileChange(path=path, action="delete", new_content="")


# --- Save ---
def test_save_creates_note_and_files_next_to_it():
    plan = SavePlan(path="domacnost/pracka.md", path_from_user=False, content="### Pračka\nBosch\n", attachments=[])
    files = [PreparedFile("zarucak", ".pdf", b"pdf"), PreparedFile("zarucak", ".pdf", b"pdf2")]
    changes = build_save_changes(plan, files, {}, {"domacnost/2026-10-30-zarucak.pdf": ("x", 1)}, DAY)
    assert changes["domacnost/2026-10-30-zarucak-2.pdf"] == b"pdf"
    assert changes["domacnost/2026-10-30-zarucak-3.pdf"] == b"pdf2"
    assert changes["domacnost/pracka.md"] == (
        "### Pračka\nBosch\n\n"
        "📎 [2026-10-30-zarucak-2.pdf](/memory/domacnost/2026-10-30-zarucak-2.pdf)\n"
        "📎 [2026-10-30-zarucak-3.pdf](/memory/domacnost/2026-10-30-zarucak-3.pdf)\n"
    )


def test_save_appends_to_existing_note():
    plan = SavePlan(path="auto/stk.md", path_from_user=True, content="nové\n", attachments=[])
    changes = build_save_changes(plan, [], {"auto/stk.md": "staré\n\n\n"}, {}, DAY)
    assert changes == {"auto/stk.md": "staré\n\nnové\n"}


# --- Edit: attachments follow notes, never deleted implicitly ---
def test_rename_where_gemini_rewrote_the_link_moves_the_pdf():
    """Regression: this exact plan used to delete the PDF as an orphan."""
    notes, attachments = notes_and_attachments()
    plan = edit(
        delete("elektronika/akaso-brave/index.md"),
        update("elektronika/akaso-brave-go-pro/index.md", NOTE.replace("akaso-brave/", "akaso-brave-go-pro/")),
    )
    prep = prepare_edit(plan, notes, attachments)
    new_pdf = "elektronika/akaso-brave-go-pro/2026-10-30-navod.pdf"
    assert prep.moves == {PDF: new_pdf}
    assert prep.deletions == [] and prep.unlinked == []
    assert f"(/memory/{new_pdf})" in prep.changes["elektronika/akaso-brave-go-pro/index.md"]

    changes = build_edit_changes(prep, notes, attachments)
    assert changes[new_pdf] == BlobRef("sha-pdf")
    assert changes[PDF] is None
    assert changes["elektronika/akaso-brave/index.md"] is None


def test_rename_with_link_kept_moves_the_pdf():
    notes, attachments = notes_and_attachments()
    plan = edit(delete("elektronika/akaso-brave/index.md"), update("elektronika/akaso-brave-go-pro/index.md", NOTE))
    prep = prepare_edit(plan, notes, attachments)
    assert prep.moves == {PDF: "elektronika/akaso-brave-go-pro/2026-10-30-navod.pdf"}
    assert prep.deletions == []


def test_dropped_link_keeps_the_file():
    notes, attachments = notes_and_attachments()
    prep = prepare_edit(edit(update("elektronika/akaso-brave/index.md", "### Akaso\nmanual\n")), notes, attachments)
    assert prep.unlinked == [PDF]
    assert prep.deletions == []
    assert PDF not in build_edit_changes(prep, notes, attachments)


def test_explicit_delete_removes_note_and_file():
    notes, attachments = notes_and_attachments()
    plan = edit(delete("elektronika/akaso-brave/index.md"), delete=[f"/memory/{PDF}"])
    prep = prepare_edit(plan, notes, attachments)
    assert prep.deletions == [PDF]
    changes = build_edit_changes(prep, notes, attachments)
    assert changes == {"elektronika/akaso-brave/index.md": None, PDF: None}


def test_delete_request_for_file_still_linked_elsewhere_is_ignored():
    notes, attachments = notes_and_attachments()
    prep = prepare_edit(edit(delete("a.md"), delete=["shared/sh.pdf"]), notes, attachments)
    assert prep.deletions == []
    assert "shared/sh.pdf" not in build_edit_changes(prep, notes, attachments)


def test_shared_attachment_is_not_moved():
    notes, attachments = notes_and_attachments()
    prep = prepare_edit(edit(update("a.md", "hi\n📎 [sh.pdf](/memory/shared/sh.pdf)\n")), notes, attachments)
    assert prep.moves == {}


def test_edit_of_note_with_old_layout_attachment_moves_it_next_to_note():
    notes = {"elektronika/akaso.md": "📎 [x.pdf](/memory/attachments/elektronika/x.pdf)\n"}
    attachments = {"attachments/elektronika/x.pdf": ("sha", 1)}
    prep = prepare_edit(edit(update("elektronika/akaso.md", "new\n📎 [x.pdf](/memory/attachments/elektronika/x.pdf)\n")),
                        notes, attachments)
    assert prep.moves == {"attachments/elektronika/x.pdf": "elektronika/x.pdf"}


def test_move_target_name_collision_gets_suffix():
    notes = {"a/n.md": "📎 [x.pdf](/memory/a/x.pdf)\n"}
    attachments = {"a/x.pdf": ("s1", 1), "b/x.pdf": ("s2", 1)}
    prep = prepare_edit(edit(delete("a/n.md"), update("b/n.md", notes["a/n.md"])), notes, attachments)
    assert prep.moves == {"a/x.pdf": "b/x-2.pdf"}


def test_gemini_cannot_touch_attachments_directly():
    notes, attachments = notes_and_attachments()
    prep = prepare_edit(edit(delete(PDF), update("auto/s.jpg", "garbage")), notes, attachments)
    assert prep.changes == {}


def test_unchanged_notes_and_new_note_paths_are_normalized():
    notes, attachments = notes_and_attachments()
    plan = edit(update("auto/stk.md", notes["auto/stk.md"] + "\n\n"), update("Zdravie/Očkovanie", "tetanus\n"))
    prep = prepare_edit(plan, notes, attachments)
    assert prep.changes == {"zdravie/ockovanie.md": "tetanus\n"}


def test_conflict_when_note_changed_since_preview():
    notes, attachments = notes_and_attachments()
    prep = prepare_edit(edit(update("auto/stk.md", "new\n")), notes, attachments)
    notes["auto/stk.md"] = "changed on github\n"
    with pytest.raises(ConflictError):
        build_edit_changes(prep, notes, attachments)


def test_conflict_when_moved_attachment_disappeared():
    notes, attachments = notes_and_attachments()
    prep = prepare_edit(edit(delete("elektronika/akaso-brave/index.md"), update("x/index.md", NOTE)), notes, attachments)
    del attachments[PDF]
    with pytest.raises(ConflictError):
        build_edit_changes(prep, notes, attachments)


def test_render_diff():
    diff = render_diff({"a.md": "one\nthree\n", "b.md": None}, {"a.md": "one\ntwo\n", "b.md": "bye\n"})
    assert "-two" in diff and "+three" in diff
    assert "--- a/b.md" in diff and "+++ /dev/null" in diff and "-bye" in diff


# --- Revert ---
def test_revert_changes_restores_parent_versions():
    commit = {"files": [
        {"filename": "memory/elektronika/new/index.md", "status": "renamed",
         "previous_filename": "memory/elektronika/old/index.md"},
        {"filename": "memory/elektronika/old/x.pdf", "status": "removed"},
        {"filename": "memory/auto/new.md", "status": "added"},
        {"filename": "memory/auto/stk.md", "status": "modified"},
        {"filename": "README.md", "status": "modified"},
    ]}
    parent = {"memory/elektronika/old/index.md": "s-idx", "memory/elektronika/old/x.pdf": "s-pdf",
              "memory/auto/stk.md": "s-stk", "README.md": "s-readme"}
    changes, skipped = revert_changes(commit, parent, "memory")
    assert changes == {
        "elektronika/new/index.md": None,
        "elektronika/old/index.md": BlobRef("s-idx"),
        "elektronika/old/x.pdf": BlobRef("s-pdf"),
        "auto/new.md": None,
        "auto/stk.md": BlobRef("s-stk"),
    }
    assert skipped == ["README.md"]
