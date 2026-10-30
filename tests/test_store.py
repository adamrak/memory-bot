"""MemoryStore against a fake GitHub API: sync, atomic commits, retries, conflicts and full user flows."""
import pytest

from memorybot.changes import (
    BlobRef, ConflictError, PreparedFile, build_edit_changes, build_save_changes, prepare_edit, revert_changes,
)
from memorybot.models import EditPlan, FileChange, SavePlan
from memorybot.store import MemoryStore
from tests.conftest import AKASO_NOTE, PDF

OLD_PDF = "elektronika/akaso-brave/2026-10-30-navod.pdf"
NEW_PDF = "elektronika/akaso-brave-go-pro/2026-10-30-navod.pdf"


async def test_sync_loads_notes_and_attachments_under_memory_dir(store):
    assert store.files == {
        "auto/stk.md": "### STK\nKončí 15. 3. 2027.\n",
        "elektronika/akaso-brave/index.md": AKASO_NOTE,
    }
    assert set(store.attachments) == {OLD_PDF}
    assert store.attachments[OLD_PDF][1] == len(PDF)
    assert store.branch == "main"


async def test_sync_is_incremental(store, github):
    reads = github.blob_reads
    assert await store.sync() is False  # nothing changed -> no tree/blob downloads
    github.push({"memory/auto/stk.md": "### STK\nKončí 2028.\n", "memory/novy.md": "nový\n"})
    assert await store.sync() is True
    assert github.blob_reads - reads == 2  # only the changed + new note
    assert store.files["auto/stk.md"] == "### STK\nKončí 2028.\n"
    assert store.files["novy.md"] == "nový\n"


async def test_apply_commits_text_binary_move_and_delete_atomically(store, github):
    before = github.head

    def build():
        return {
            "auto/stk.md": "updated\n",
            "auto/photo.jpg": b"\xff\xd8jpeg",
            NEW_PDF: BlobRef(store.attachments[OLD_PDF][0]),
            OLD_PDF: None,
        }

    url = await store.apply(build, "test commit")
    assert url == f"https://github.com/owner/repo/commit/{github.head}"
    assert github.commits[github.head]["parent"] == before  # one commit on top
    files = github.files()
    assert files["memory/auto/stk.md"] == b"updated\n"
    assert files["memory/auto/photo.jpg"] == b"\xff\xd8jpeg"
    assert files[f"memory/{NEW_PDF}"] == PDF
    assert f"memory/{OLD_PDF}" not in files
    assert files["README.md"] == b"repo readme, outside memory/"
    # cache follows the commit
    assert store.sha == github.head
    assert store.files["auto/stk.md"] == "updated\n"
    assert NEW_PDF in store.attachments and OLD_PDF not in store.attachments


async def test_apply_rebuilds_on_top_of_concurrent_push(store, github):
    github.before_ref_update = lambda: github.push({"memory/auto/stk.md": "edited on github\n"})
    builds = []

    def build():
        builds.append(store.files["auto/stk.md"])
        existing = store.files["auto/stk.md"]
        return {"auto/stk.md": existing + "appended by bot\n"}

    await store.apply(build, "append")
    assert len(builds) == 2  # first attempt rejected (not a fast forward), rebuilt on fresh state
    assert github.files()["memory/auto/stk.md"] == b"edited on github\nappended by bot\n"


async def test_apply_with_nothing_to_change_raises(store):
    with pytest.raises(ValueError):
        await store.apply(lambda: {}, "noop")


async def test_save_flow_end_to_end(store, github):
    plan = SavePlan(path="domacnost/pracka.md", path_from_user=False, content="### Pračka\nBosch\n", attachments=[])
    files = [PreparedFile("zarucak", ".pdf", b"warranty")]
    await store.apply(lambda: build_save_changes(plan, files, store.files, store.attachments), "save")
    note = github.files()["memory/domacnost/pracka.md"].decode()
    assert note.startswith("### Pračka\nBosch\n\n📎 [")
    linked = [p for p in store.attachments if p.startswith("domacnost/")]
    assert len(linked) == 1 and github.files()[f"memory/{linked[0]}"] == b"warranty"


async def test_rename_flow_keeps_pdf(store, github):
    """Regression for the folder rename that deleted the manual."""
    plan = EditPlan(summary="rename", delete_attachments=[], changes=[
        FileChange(path="elektronika/akaso-brave/index.md", action="delete", new_content=""),
        FileChange(path="elektronika/akaso-brave-go-pro/index.md", action="update",
                   new_content=AKASO_NOTE.replace("akaso-brave/", "akaso-brave-go-pro/")),
    ])
    prep = prepare_edit(plan, store.files, store.attachments)
    await store.apply(lambda: build_edit_changes(prep, store.files, store.attachments), "rename")
    files = github.files()
    assert files[f"memory/{NEW_PDF}"] == PDF
    assert f"(/memory/{NEW_PDF})" in files["memory/elektronika/akaso-brave-go-pro/index.md"].decode()
    assert not any(p.startswith("memory/elektronika/akaso-brave/") for p in files)


async def test_edit_conflict_when_note_changed_on_github(store, github):
    plan = EditPlan(summary="x", delete_attachments=[],
                    changes=[FileChange(path="auto/stk.md", action="update", new_content="bot edit\n")])
    prep = prepare_edit(plan, store.files, store.attachments)
    github.push({"memory/auto/stk.md": "edited on github\n"})
    with pytest.raises(ConflictError):
        await store.apply(lambda: build_edit_changes(prep, store.files, store.attachments), "edit")
    assert github.files()["memory/auto/stk.md"] == b"edited on github\n"


async def test_revert_flow_restores_deleted_pdf(store, github):
    await store.apply(lambda: {OLD_PDF: None, "auto/stk.md": "oops\n"}, "bad change")

    commit = await store.head_commit()
    parent = await store.tree_blobs(commit["parents"][0]["sha"])
    changes, skipped = revert_changes(commit, parent, store.memory_dir)
    assert skipped == []
    await store.apply(lambda: changes, "revert")

    files = github.files()
    assert files[f"memory/{OLD_PDF}"] == PDF
    assert files["memory/auto/stk.md"] == "### STK\nKončí 15. 3. 2027.\n".encode()


async def test_store_on_repo_root(github):
    s = MemoryStore("owner/repo", "token", memory_dir="", api_url=github.url)
    await s.start()
    try:
        await s.sync()
        assert "README.md" in s.files and "memory/auto/stk.md" in s.files
        assert s.repo_path("x.md") == "x.md"
    finally:
        await s.close()
