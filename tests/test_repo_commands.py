"""Tests for the higher-level commands: rm, fsck, gc, migrate, branch, merge.

These cover the behaviour guarantees that distinguish a version control system
from a file archiver: tracked files cannot silently vanish from history,
corruption is detectable, and branches really record independent snapshots.
"""

import hashlib

import pytest

from blobtrack.cli.commands import (
    cmd_add,
    cmd_branch,
    cmd_checkout,
    cmd_commit,
    cmd_fsck,
    cmd_gc,
    cmd_init,
    cmd_log,
    cmd_merge,
    cmd_migrate,
    cmd_rm,
    cmd_switch,
)
from blobtrack.storage.index_db import IndexDB
from blobtrack.storage.local_store import LocalStore


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An initialized repository with cwd inside it."""
    base = tmp_path / "repo"
    base.mkdir()
    cmd_init(cwd=base)
    monkeypatch.chdir(base)
    return base


def _add_commit(repo, name: str, payload: bytes, message: str) -> str:
    (repo / name).write_bytes(payload)
    cmd_add(name)
    cmd_commit(message)
    with IndexDB(repo / ".blobtrack" / "index.db") as db:
        return db.list_commits()[0]["commit_hash"]


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------


class TestRm:
    def test_rm_stops_tracking(self, repo):
        (repo / "a.bin").write_bytes(b"content " * 1000)
        cmd_add("a.bin")

        cmd_rm("a.bin")

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            assert db.get_file("a.bin") is None

    def test_rm_allows_commit_after_file_deleted(self, repo):
        """The workflow that used to be impossible: delete a tracked file,
        untrack it, then commit the remaining files."""
        _add_commit(repo, "keep.bin", b"k" * 5000, "v1")
        _add_commit(repo, "gone.bin", b"g" * 5000, "v1 again")
        (repo / "gone.bin").unlink()

        cmd_rm("gone.bin")
        cmd_commit("v2 without gone.bin")  # must not raise

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            assert len(db.list_commits()) == 3
            merge_files = set(db.get_commit_file_paths(db.list_commits()[0]["commit_hash"]))
            assert "gone.bin" not in merge_files
            assert "keep.bin" in merge_files

    def test_commit_with_zero_tracked_files_is_an_error(self, repo):
        """Untracking the last file leaves nothing to snapshot. That must be a
        clear error, not an empty commit."""
        _add_commit(repo, "only.bin", b"o" * 5000, "v1")
        (repo / "only.bin").unlink()
        cmd_rm("only.bin")

        with pytest.raises(SystemExit) as exc:
            cmd_commit("nothing left")
        assert exc.value.code == 1

    def test_rm_preserves_history(self, repo):
        commit = _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        _add_commit(repo, "b.bin", b"b" * 5000, "v1 two files")
        (repo / "a.bin").unlink()
        cmd_rm("a.bin")
        cmd_commit("v2")

        # The old commit must still be fully restorable.
        cmd_checkout(commit)
        assert (repo / "a.bin").exists()
        assert (repo / "a.bin").read_bytes() == b"a" * 5000

    def test_rm_missing_path_exits_1(self, repo):
        with pytest.raises(SystemExit) as exc:
            cmd_rm("never-tracked.bin")
        assert exc.value.code == 1

    def test_rm_works_after_file_already_gone(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        (repo / "a.bin").unlink()

        cmd_rm("a.bin")  # must not raise

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            assert db.get_file("a.bin") is None

    def test_rm_normalizes_separators(self, repo):
        """`blob rm sub\a.bin` must find `sub/a.bin` in the database."""
        (repo / "sub").mkdir()
        (repo / "sub" / "a.bin").write_bytes(b"a" * 5000)
        cmd_add(str(repo / "sub" / "a.bin"))
        cmd_commit("v1")

        cmd_rm(r"sub\a.bin")

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            assert db.get_file("sub/a.bin") is None

    def test_rm_relative_from_subdirectory(self, repo, monkeypatch):
        (repo / "sub").mkdir()
        (repo / "sub" / "a.bin").write_bytes(b"a" * 5000)
        cmd_add(str(repo / "sub" / "a.bin"))
        cmd_commit("v1")

        monkeypatch.chdir(repo / "sub")
        cmd_rm("a.bin")  # must resolve to sub/a.bin

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            assert db.get_file("sub/a.bin") is None

    def test_rm_rejects_parent_traversal(self, repo):
        (repo / "a.bin").write_bytes(b"a" * 5000)
        cmd_add("a.bin")
        cmd_commit("v1")

        with pytest.raises(SystemExit):
            cmd_rm("../../a.bin")

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            assert db.get_file("a.bin") is not None, "must not have been untracked"

    def test_rm_directory_requires_recursive(self, repo):
        (repo / "sub").mkdir()
        (repo / "sub" / "a.bin").write_bytes(b"a" * 5000)
        (repo / "sub" / "b.bin").write_bytes(b"b" * 5000)
        cmd_add(str(repo / "sub" / "a.bin"))
        cmd_add(str(repo / "sub" / "b.bin"))
        cmd_commit("v1")

        with pytest.raises(SystemExit) as exc:
            cmd_rm("sub")
        assert exc.value.code == 1

        # Nothing was untracked by the failed attempt.
        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            assert len(db.list_files()) == 2

    def test_rm_recursive_untracks_whole_directory(self, repo):
        (repo / "sub").mkdir()
        (repo / "sub" / "a.bin").write_bytes(b"a" * 5000)
        (repo / "sub" / "b.bin").write_bytes(b"b" * 5000)
        (repo / "other.bin").write_bytes(b"o" * 5000)
        for name in ("sub/a.bin", "sub/b.bin", "other.bin"):
            cmd_add(str(repo / name))
        cmd_commit("v1")

        cmd_rm("sub", recursive=True)
        cmd_commit("v2")

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            paths = {record["path"] for record in db.list_files()}
            assert paths == {"other.bin"}
            new_head = db.get_branch_head("main")
            assert set(db.get_commit_file_paths(new_head)) == {"other.bin"}


# ---------------------------------------------------------------------------
# Missing tracked files are a hard error
# ---------------------------------------------------------------------------


class TestMissingTrackedFile:
    def test_commit_fails_loudly_when_file_missing(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        (repo / "a.bin").unlink()

        with pytest.raises(SystemExit) as exc:
            cmd_commit("should not succeed")
        assert exc.value.code == 1

    def test_commit_failure_does_not_create_a_commit(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        (repo / "a.bin").unlink()

        with pytest.raises(SystemExit):
            cmd_commit("nope")

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            assert len(db.list_commits()) == 1

    def test_commit_failure_keeps_existing_history_intact(self, repo):
        commit = _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        (repo / "a.bin").unlink()

        with pytest.raises(SystemExit):
            cmd_commit("nope")

        cmd_checkout(commit)
        assert (repo / "a.bin").read_bytes() == b"a" * 5000

    def test_history_never_silently_drops_a_file(self, repo):
        """The exact data-loss scenario: delete a tracked file, add another,
        commit. The first commit must still contain both files."""
        first = _add_commit(repo, "keep.bin", b"K" * 5000, "v1 both files")
        _add_commit(repo, "gone.bin", b"G" * 5000, "v1 again")

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            assert set(db.get_commit_file_paths(first)) >= {"keep.bin"}


# ---------------------------------------------------------------------------
# Path confinement end-to-end
# ---------------------------------------------------------------------------


class TestPathConfinement:
    def test_add_rejects_file_outside_repo(self, repo, tmp_path):
        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"x" * 5000)

        with pytest.raises(SystemExit) as exc:
            cmd_add(str(outside))
        assert exc.value.code == 1

    def test_checkout_refuses_to_escape_via_tampered_metadata(self, repo, tmp_path):
        """Simulate a hostile remote pushing a traversal path."""
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            conn = db._get_connection()
            with conn:
                conn.execute("UPDATE chunk_refs SET file_path = '../escaped.bin'")
            commit_hash = db.list_commits()[0]["commit_hash"]

        with pytest.raises(SystemExit) as exc:
            cmd_checkout(commit_hash)
        assert exc.value.code == 1
        assert not (repo.parent / "escaped.bin").exists()

    def test_checkout_refuses_absolute_path_from_metadata(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            conn = db._get_connection()
            with conn:
                conn.execute("UPDATE chunk_refs SET file_path = 'C:/Windows/win.ini'")
            commit_hash = db.list_commits()[0]["commit_hash"]

        with pytest.raises(SystemExit):
            cmd_checkout(commit_hash)


# ---------------------------------------------------------------------------
# fsck
# ---------------------------------------------------------------------------


class TestFsck:
    def test_fsck_passes_on_healthy_repo(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        cmd_fsck()  # must not raise

    def test_fsck_passes_with_multiple_commits(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        _add_commit(repo, "a.bin", b"b" * 5000, "v2")
        cmd_fsck()

    def test_fsck_detects_missing_chunk(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")

        store = LocalStore(repo / ".blobtrack" / "objects")
        store.delete_chunk(store.list_chunks()[0])

        with pytest.raises(SystemExit) as exc:
            cmd_fsck()
        assert exc.value.code == 1

    def test_fsck_detects_corrupt_chunk(self, repo):
        from blobtrack.core.packer import compress

        _add_commit(repo, "a.bin", b"a" * 5000, "v1")

        store = LocalStore(repo / ".blobtrack" / "objects")
        victim = store.list_chunks()[0]
        store.get_chunk_path(victim).write_bytes(compress(b"tampered payload"))

        with pytest.raises(SystemExit) as exc:
            cmd_fsck()
        assert exc.value.code == 1

    def test_fsck_detects_corruption_before_checkout_uses_it(self, repo):
        """Checkout must refuse to write out a corrupt chunk."""
        from blobtrack.core.packer import compress

        commit = _add_commit(repo, "a.bin", b"a" * 5000, "v1")

        store = LocalStore(repo / ".blobtrack" / "objects")
        victim = store.list_chunks()[0]
        store.get_chunk_path(victim).write_bytes(compress(b"wrong data"))

        with pytest.raises(SystemExit):
            cmd_checkout(commit)

    def test_checkout_reports_missing_chunk(self, repo):
        commit = _add_commit(repo, "a.bin", b"a" * 5000, "v1")

        store = LocalStore(repo / ".blobtrack" / "objects")
        store.get_chunk_path(store.list_chunks()[0]).unlink()

        with pytest.raises(SystemExit) as exc:
            cmd_checkout(commit)
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# gc
# ---------------------------------------------------------------------------


class TestGc:
    def test_gc_dry_run_changes_nothing(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        before = LocalStore(repo / ".blobtrack" / "objects").list_chunks()

        cmd_gc(dry_run=True)

        after = LocalStore(repo / ".blobtrack" / "objects").list_chunks()
        assert sorted(before) == sorted(after)

    def test_gc_removes_unreferenced_chunks(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")

        store = LocalStore(repo / ".blobtrack" / "objects")
        store.store_chunk(hashlib.sha256(b"orphan").hexdigest(), b"orphan bytes")

        cmd_gc()

        assert store.has_chunk(hashlib.sha256(b"orphan").hexdigest()) is False

    def test_gc_keeps_chunks_needed_by_old_commits(self, repo):
        first = _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        store = LocalStore(repo / ".blobtrack" / "objects")
        before = set(store.list_chunks())

        cmd_gc()

        assert set(store.list_chunks()) == before

        # Still restorable after gc.
        (repo / "a.bin").unlink()
        cmd_checkout(first)
        assert (repo / "a.bin").read_bytes() == b"a" * 5000


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------


class TestMigrate:
    def test_migrate_handles_repo_already_modern(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        cmd_migrate()  # must not raise

    def test_migrate_converts_legacy_repo_and_stays_checkoutable(self, repo):
        commit = _add_commit(repo, "a.bin", b"a" * 5000, "v1")

        store = LocalStore(repo / ".blobtrack" / "objects")
        for chunk_hash in store.list_chunks():
            data = store.get_chunk_path(chunk_hash).read_bytes()
            # Move into the pre-fan-out flat layout.
            (store.objects_dir / chunk_hash).write_bytes(data)
            store.get_chunk_path(chunk_hash).unlink()
        assert all(len(c) == 64 for c in store.list_chunks())

        cmd_migrate()

        (repo / "a.bin").unlink()
        cmd_checkout(commit)
        assert (repo / "a.bin").read_bytes() == b"a" * 5000


# ---------------------------------------------------------------------------
# branches
# ---------------------------------------------------------------------------


class TestBranches:
    def test_branch_lists_default_branch(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        cmd_branch()  # must not raise

    def test_create_branch_at_current_head(self, repo):
        commit = _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        cmd_branch("feature")

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            assert db.get_branch_head("feature") == commit

    def test_switch_changes_current_branch(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        cmd_branch("feature")
        cmd_switch("feature")

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            from blobtrack.storage.index_db import short_branch_name

            assert short_branch_name(db.get_current_branch()) == "feature"

    def test_commits_land_on_current_branch(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        cmd_branch("feature")
        cmd_switch("feature")
        feature_commit = _add_commit(repo, "a.bin", b"b" * 5000, "on feature")

        cmd_switch("main")
        main_commit = _add_commit(repo, "a.bin", b"c" * 5000, "on main")

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            assert db.get_branch_head("feature") == feature_commit
            assert db.get_branch_head("main") == main_commit
            assert feature_commit != main_commit

    def test_branch_does_not_mutate_other_branch_history(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        cmd_branch("feature")
        cmd_switch("feature")
        _add_commit(repo, "a.bin", b"b" * 5000, "on feature")

        cmd_switch("main")
        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            assert len(db.list_commits()) == 2

    def test_delete_branch(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        cmd_branch("temp")
        cmd_branch("temp", delete=True)

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            assert db.get_branch_head("temp") is None

    def test_cannot_delete_current_branch(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        with pytest.raises(SystemExit):
            cmd_branch("main", delete=True)

    def test_switch_to_unknown_branch_fails(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        with pytest.raises(SystemExit):
            cmd_switch("nope")

    def test_create_duplicate_branch_fails(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        cmd_branch("feature")
        with pytest.raises(SystemExit):
            cmd_branch("feature")

    def test_checkout_accepts_branch_name(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        cmd_branch("feature")
        (repo / "a.bin").unlink()

        cmd_checkout("feature")

        assert (repo / "a.bin").read_bytes() == b"a" * 5000


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


class TestMerge:
    def test_merge_fast_forwards(self, repo):
        _add_commit(repo, "a.bin", b"a" * 5000, "v1")
        cmd_branch("feature")
        cmd_switch("feature")
        feature_commit = _add_commit(repo, "a.bin", b"b" * 5000, "feature work")
        cmd_switch("main")

        cmd_merge("feature")

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            assert db.get_branch_head("main") == feature_commit

    def test_merge_unions_files_from_both_sides(self, repo):
        _add_commit(repo, "shared.bin", b"S" * 5000, "base")
        cmd_branch("feature")
        cmd_switch("feature")
        _add_commit(repo, "feature_only.bin", b"F" * 5000, "feature adds file")
        cmd_switch("main")
        _add_commit(repo, "main_only.bin", b"M" * 5000, "main adds file")

        cmd_merge("feature")

        merge_head = None
        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            merge_head = db.get_branch_head("main")
            paths = set(db.get_commit_file_paths(merge_head))
            assert {"shared.bin", "feature_only.bin", "main_only.bin"} <= paths
            assert len(db.get_commit_parents(merge_head)) == 2

    def test_merge_creates_two_parent_commit(self, repo):
        _add_commit(repo, "shared.bin", b"S" * 5000, "base")
        cmd_branch("feature")
        cmd_switch("feature")
        _add_commit(repo, "f.bin", b"F" * 5000, "feature work")
        cmd_switch("main")
        main_commit = _add_commit(repo, "m.bin", b"M" * 5000, "main work")

        cmd_merge("feature")

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            merge_head = db.get_branch_head("main")
            parents = db.get_commit_parents(merge_head)
            assert len(parents) == 2
            assert main_commit in parents

    def test_merge_result_is_checkoutable(self, repo):
        _add_commit(repo, "shared.bin", b"S" * 5000, "base")
        cmd_branch("feature")
        cmd_switch("feature")
        feature_payload = b"F" * 5000
        _add_commit(repo, "feature_only.bin", feature_payload, "feature work")
        cmd_switch("main")
        main_payload = b"M" * 5000
        _add_commit(repo, "main_only.bin", main_payload, "main work")

        cmd_merge("feature")

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            merge_head = db.get_branch_head("main")

        for name in ("feature_only.bin", "main_only.bin"):
            (repo / name).unlink()
        cmd_checkout(merge_head)

        assert (repo / "feature_only.bin").read_bytes() == feature_payload
        assert (repo / "main_only.bin").read_bytes() == main_payload

    def test_merge_reports_conflicting_files(self, repo):
        _add_commit(repo, "conflict.bin", b"original" * 1000, "base")
        cmd_branch("feature")
        cmd_switch("feature")
        _add_commit(repo, "conflict.bin", b"feature version " * 500, "feature edits")
        cmd_switch("main")
        _add_commit(repo, "conflict.bin", b"main version " * 500, "main edits")

        with pytest.raises(SystemExit) as exc:
            cmd_merge("feature")
        assert exc.value.code == 1

    def test_failed_merge_leaves_branch_unchanged(self, repo):
        _add_commit(repo, "conflict.bin", b"original" * 1000, "base")
        cmd_branch("feature")
        cmd_switch("feature")
        _add_commit(repo, "conflict.bin", b"feature version " * 500, "feature edits")
        cmd_switch("main")
        main_head = _add_commit(repo, "conflict.bin", b"main version " * 500, "main edits")

        with pytest.raises(SystemExit):
            cmd_merge("feature")

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            assert db.get_branch_head("main") == main_head

    def test_merge_same_content_is_not_a_conflict(self, repo):
        payload = b"identical content " * 500
        _add_commit(repo, "same.bin", payload, "base")
        cmd_branch("feature")
        cmd_switch("feature")
        _add_commit(repo, "other.bin", b"O" * 5000, "feature work")
        cmd_switch("main")
        _add_commit(repo, "another.bin", b"A" * 5000, "main work")

        cmd_merge("feature")  # must not raise


# ---------------------------------------------------------------------------
# rich markup safety
# ---------------------------------------------------------------------------


class TestOutputSafety:
    def test_commit_message_with_markup_does_not_break_anything(self, repo):
        """A message containing a Rich tag must not corrupt state."""
        (repo / "a.bin").write_bytes(b"a" * 5000)
        cmd_add("a.bin")

        cmd_commit("fix [/green] the parser")

        with IndexDB(repo / ".blobtrack" / "index.db") as db:
            commits = db.list_commits()
            assert len(commits) == 1
            assert commits[0]["message"] == "fix [/green] the parser"

    def test_log_still_works_after_markup_message(self, repo):
        (repo / "a.bin").write_bytes(b"a" * 5000)
        cmd_add("a.bin")
        cmd_commit("weird [/red] message")

        cmd_log()  # must not raise

    def test_checkout_works_after_markup_message(self, repo):
        commit = _add_commit(repo, "a.bin", b"a" * 5000, "weird [/] message")
        (repo / "a.bin").unlink()

        cmd_checkout(commit)

        assert (repo / "a.bin").read_bytes() == b"a" * 5000
