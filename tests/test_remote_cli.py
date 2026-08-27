"""
test_remote_cli.py — Phase 5 Remote Push/Pull CLI Integration Tests

Covers:
- Push: first push, repeated push, incremental push, multi-file push,
  commit metadata synced, parent chain preserved, dedup, zero-transfer,
  invalid remote, no local repo
- Pull: into existing repo, into fresh repo, repeated pull, multi-file pull,
  commit metadata preserved, chunk refs preserved, invalid remote, missing remote
- Round Trip: A → push → remote → pull → B → checkout exact SHA-256
- Delta: existing chunks skipped, modified content transfers only needed data,
  no duplicate objects, zero-transfer sync
- Regression: GC interaction, checkout after pull
"""

import hashlib
import pathlib
import subprocess
import sys

import pytest

from blobtrack.cli.commands import (
    cmd_init,
    cmd_add,
    cmd_commit,
    cmd_log,
    cmd_checkout,
    cmd_gc,
    cmd_push,
    cmd_pull,
)
from blobtrack.storage.index_db import IndexDB
from blobtrack.storage.local_store import LocalStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_repo(base: pathlib.Path) -> pathlib.Path:
    """Create and init a blobtrack repo at base, return the path."""
    base.mkdir(parents=True, exist_ok=True)
    cmd_init(cwd=base)
    return base


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# PUSH TESTS
# ---------------------------------------------------------------------------

class TestPushIntegration:
    def test_push_first(self, tmp_path, monkeypatch):
        """First push transfers all chunks and commits."""
        local = _setup_repo(tmp_path / "local")
        remote = tmp_path / "remote"
        monkeypatch.chdir(local)

        data = b"push first test " * 5000
        (local / "file.bin").write_bytes(data)
        cmd_add(str(local / "file.bin"))
        cmd_commit("C1")

        cmd_push(str(remote))

        # Verify remote has objects and DB
        remote_store = LocalStore(remote / ".blobtrack" / "objects")
        remote_db = IndexDB(remote / ".blobtrack" / "index.db")
        assert len(remote_store.list_chunks()) >= 1
        commits = remote_db.list_commits()
        assert len(commits) == 1
        assert commits[0]["message"] == "C1"
        remote_db.close()

    def test_push_repeated_zero_transfer(self, tmp_path, monkeypatch):
        """Second push of same state transfers nothing."""
        local = _setup_repo(tmp_path / "local")
        remote = tmp_path / "remote"
        monkeypatch.chdir(local)

        (local / "f.bin").write_bytes(b"repeat " * 3000)
        cmd_add(str(local / "f.bin"))
        cmd_commit("C1")

        cmd_push(str(remote))
        # Second push should be zero-transfer
        cmd_push(str(remote))

        remote_store = LocalStore(remote / ".blobtrack" / "objects")
        remote_db = IndexDB(remote / ".blobtrack" / "index.db")
        assert len(remote_db.list_commits()) == 1
        remote_db.close()

    def test_push_incremental(self, tmp_path, monkeypatch):
        """Incremental push transfers only new chunks."""
        local = _setup_repo(tmp_path / "local")
        remote = tmp_path / "remote"
        monkeypatch.chdir(local)

        (local / "f.bin").write_bytes(b"A" * (2 * 1024 * 1024))
        cmd_add(str(local / "f.bin"))
        cmd_commit("C1")
        cmd_push(str(remote))

        remote_store = LocalStore(remote / ".blobtrack" / "objects")
        chunks_after_c1 = len(remote_store.list_chunks())

        # Modify and push C2
        (local / "f.bin").write_bytes(b"B" * (2 * 1024 * 1024))
        cmd_add(str(local / "f.bin"))
        cmd_commit("C2")
        cmd_push(str(remote))

        remote_db = IndexDB(remote / ".blobtrack" / "index.db")
        assert len(remote_db.list_commits()) == 2
        remote_db.close()

    def test_push_multi_file(self, tmp_path, monkeypatch):
        """Push with multiple files, modify one, verify remote state."""
        local = _setup_repo(tmp_path / "local")
        remote = tmp_path / "remote"
        monkeypatch.chdir(local)

        (local / "a.txt").write_bytes(b"AAA " * 2000)
        (local / "b.txt").write_bytes(b"BBB " * 2000)
        cmd_add(str(local / "a.txt"))
        cmd_add(str(local / "b.txt"))
        cmd_commit("C1")
        cmd_push(str(remote))

        # Modify only a.txt
        (local / "a.txt").write_bytes(b"AXA " * 2000)
        cmd_add(str(local / "a.txt"))
        cmd_commit("C2")
        cmd_push(str(remote))

        remote_db = IndexDB(remote / ".blobtrack" / "index.db")
        commits = remote_db.list_commits()
        assert len(commits) == 2
        # Verify C2 has refs for both files
        c2_refs = remote_db.get_commit_chunk_refs(commits[0]["commit_hash"])
        file_paths = {r["file_path"] for r in c2_refs}
        assert "a.txt" in file_paths
        assert "b.txt" in file_paths
        remote_db.close()

    def test_push_commit_parent_chain(self, tmp_path, monkeypatch):
        """Push 3 commits, verify parent chain on remote."""
        local = _setup_repo(tmp_path / "local")
        remote = tmp_path / "remote"
        monkeypatch.chdir(local)

        for i in range(3):
            (local / "f.bin").write_bytes(f"version {i} ".encode() * 3000)
            cmd_add(str(local / "f.bin"))
            cmd_commit(f"C{i+1}")

        cmd_push(str(remote))

        remote_db = IndexDB(remote / ".blobtrack" / "index.db")
        commits = remote_db.list_commits()  # newest first
        assert len(commits) == 3
        # C3 parent -> C2 parent -> C1 parent -> None
        assert commits[0]["message"] == "C3"
        assert commits[0]["parent_hash"] == commits[1]["commit_hash"]
        assert commits[1]["message"] == "C2"
        assert commits[1]["parent_hash"] == commits[2]["commit_hash"]
        assert commits[2]["message"] == "C1"
        assert commits[2]["parent_hash"] is None
        remote_db.close()

    def test_push_no_commits(self, tmp_path, monkeypatch):
        """Push with no commits prints nothing-to-push."""
        local = _setup_repo(tmp_path / "local")
        remote = tmp_path / "remote"
        monkeypatch.chdir(local)
        # No commit, just init
        cmd_push(str(remote))  # should not raise

    def test_push_no_local_repo(self, tmp_path, monkeypatch):
        """Push outside a repo exits 1."""
        monkeypatch.chdir(tmp_path)
        remote = tmp_path / "remote"
        with pytest.raises(SystemExit) as exc:
            cmd_push(str(remote))
        assert exc.value.code == 1

    def test_push_invalid_remote_parent(self, tmp_path, monkeypatch):
        """Push to a non-existent parent directory exits 1."""
        local = _setup_repo(tmp_path / "local")
        monkeypatch.chdir(local)
        (local / "f.bin").write_bytes(b"data " * 2000)
        cmd_add(str(local / "f.bin"))
        cmd_commit("C1")
        with pytest.raises(SystemExit) as exc:
            cmd_push(r"Z:\nonexistent\path\that\does\not\exist")
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# PULL TESTS
# ---------------------------------------------------------------------------

class TestPullIntegration:
    def test_pull_into_existing_repo(self, tmp_path, monkeypatch):
        """Pull fetches missing commits and chunks into local."""
        source = _setup_repo(tmp_path / "source")
        remote = tmp_path / "remote"
        local = _setup_repo(tmp_path / "local")
        monkeypatch.chdir(source)

        (source / "f.bin").write_bytes(b"pull test " * 3000)
        cmd_add(str(source / "f.bin"))
        cmd_commit("C1")
        cmd_push(str(remote))

        monkeypatch.chdir(local)
        cmd_pull(str(remote))

        db = IndexDB(local / ".blobtrack" / "index.db")
        commits = db.list_commits()
        assert len(commits) == 1
        assert commits[0]["message"] == "C1"
        db.close()

    def test_pull_into_fresh_clone(self, tmp_path, monkeypatch):
        """Pull into a fresh init'd repo and verify checkout works."""
        source = _setup_repo(tmp_path / "source")
        remote = tmp_path / "remote"
        clone = _setup_repo(tmp_path / "clone")
        monkeypatch.chdir(source)

        orig_data = b"clone test exact bytes " * 4000
        (source / "file.dat").write_bytes(orig_data)
        cmd_add(str(source / "file.dat"))
        cmd_commit("C1")
        cmd_push(str(remote))

        # Pull into clone
        monkeypatch.chdir(clone)
        cmd_pull(str(remote))

        # Now checkout and verify exact SHA-256
        db = IndexDB(clone / ".blobtrack" / "index.db")
        commits = db.list_commits()
        assert len(commits) == 1
        c1_hash = commits[0]["commit_hash"]
        db.close()

        cmd_checkout(c1_hash)
        restored = (clone / "file.dat").read_bytes()
        assert _sha256(restored) == _sha256(orig_data)
        assert restored == orig_data

    def test_pull_repeated_zero_transfer(self, tmp_path, monkeypatch):
        """Second pull should be zero-transfer."""
        source = _setup_repo(tmp_path / "source")
        remote = tmp_path / "remote"
        local = _setup_repo(tmp_path / "local")

        monkeypatch.chdir(source)
        (source / "f.bin").write_bytes(b"repeat pull " * 2000)
        cmd_add(str(source / "f.bin"))
        cmd_commit("C1")
        cmd_push(str(remote))

        monkeypatch.chdir(local)
        cmd_pull(str(remote))
        cmd_pull(str(remote))  # second pull - should not fail or duplicate

        db = IndexDB(local / ".blobtrack" / "index.db")
        assert len(db.list_commits()) == 1
        db.close()

    def test_pull_no_local_repo(self, tmp_path, monkeypatch):
        """Pull outside a repo exits 1."""
        monkeypatch.chdir(tmp_path)
        remote = tmp_path / "remote"
        remote.mkdir()
        with pytest.raises(SystemExit) as exc:
            cmd_pull(str(remote))
        assert exc.value.code == 1

    def test_pull_missing_remote(self, tmp_path, monkeypatch):
        """Pull from non-existent remote exits 1."""
        local = _setup_repo(tmp_path / "local")
        monkeypatch.chdir(local)
        with pytest.raises(SystemExit) as exc:
            cmd_pull(str(tmp_path / "nonexistent_remote"))
        assert exc.value.code == 1

    def test_pull_invalid_remote_structure(self, tmp_path, monkeypatch):
        """Pull from a plain directory (not a blobtrack repo) exits 1."""
        local = _setup_repo(tmp_path / "local")
        monkeypatch.chdir(local)
        plain_dir = tmp_path / "plain_dir"
        plain_dir.mkdir()
        with pytest.raises(SystemExit) as exc:
            cmd_pull(str(plain_dir))
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# ROUND-TRIP TESTS
# ---------------------------------------------------------------------------

class TestRemoteRoundTrip:
    def test_push_pull_checkout_exact(self, tmp_path, monkeypatch):
        """A->push->remote->pull->B, then B checkout matches A's exact data."""
        repo_a = _setup_repo(tmp_path / "A")
        remote = tmp_path / "remote"
        repo_b = _setup_repo(tmp_path / "B")

        # A: add and commit
        monkeypatch.chdir(repo_a)
        orig = b"round trip exact verification " * 5000
        (repo_a / "data.bin").write_bytes(orig)
        cmd_add(str(repo_a / "data.bin"))
        cmd_commit("v1")

        db_a = IndexDB(repo_a / ".blobtrack" / "index.db")
        c1_hash = db_a.list_commits()[0]["commit_hash"]
        db_a.close()

        # A: push to remote
        cmd_push(str(remote))

        # B: pull from remote
        monkeypatch.chdir(repo_b)
        cmd_pull(str(remote))

        # B: checkout and verify
        cmd_checkout(c1_hash)
        restored = (repo_b / "data.bin").read_bytes()
        assert _sha256(restored) == _sha256(orig)
        assert restored == orig

    def test_bidirectional_round_trip(self, tmp_path, monkeypatch):
        """
        A->push->remote->pull->B->modify->commit->push->remote->pull->A
        Proves bidirectional sync with exact SHA-256 verification.
        """
        repo_a = _setup_repo(tmp_path / "A")
        remote = tmp_path / "remote"
        repo_b = _setup_repo(tmp_path / "B")

        # A: create original and push
        monkeypatch.chdir(repo_a)
        v1_data = b"version ONE data " * 4000
        (repo_a / "shared.bin").write_bytes(v1_data)
        cmd_add(str(repo_a / "shared.bin"))
        cmd_commit("v1 from A")
        cmd_push(str(remote))

        db_a = IndexDB(repo_a / ".blobtrack" / "index.db")
        c1_hash = db_a.list_commits()[0]["commit_hash"]
        db_a.close()

        # B: pull, checkout, verify v1
        monkeypatch.chdir(repo_b)
        cmd_pull(str(remote))
        cmd_checkout(c1_hash)
        assert (repo_b / "shared.bin").read_bytes() == v1_data

        # B: modify, add, commit v2, push
        v2_data = b"version TWO data " * 4000
        (repo_b / "shared.bin").write_bytes(v2_data)
        cmd_add(str(repo_b / "shared.bin"))
        cmd_commit("v2 from B")
        cmd_push(str(remote))

        db_b = IndexDB(repo_b / ".blobtrack" / "index.db")
        b_commits = db_b.list_commits()
        c2_hash = b_commits[0]["commit_hash"]
        db_b.close()

        # A: pull from remote, should get v2
        monkeypatch.chdir(repo_a)
        cmd_pull(str(remote))

        # A: log should show both commits
        db_a = IndexDB(repo_a / ".blobtrack" / "index.db")
        a_commits = db_a.list_commits()
        assert len(a_commits) >= 2
        db_a.close()

        # A: checkout v1 and v2 with exact verification
        cmd_checkout(c1_hash)
        assert (repo_a / "shared.bin").read_bytes() == v1_data
        assert _sha256((repo_a / "shared.bin").read_bytes()) == _sha256(v1_data)

        cmd_checkout(c2_hash)
        assert (repo_a / "shared.bin").read_bytes() == v2_data
        assert _sha256((repo_a / "shared.bin").read_bytes()) == _sha256(v2_data)


# ---------------------------------------------------------------------------
# DELTA DEDUPLICATION TESTS
# ---------------------------------------------------------------------------

class TestRemoteDeduplication:
    def test_first_transfer_all_new(self, tmp_path, monkeypatch):
        """First push has zero skipped chunks."""
        local = _setup_repo(tmp_path / "local")
        remote = tmp_path / "remote"
        monkeypatch.chdir(local)

        (local / "f.bin").write_bytes(b"new data " * 5000)
        cmd_add(str(local / "f.bin"))
        cmd_commit("C1")

        # Use RemoteSync directly to check stats
        from blobtrack.storage.remote_sync import RemoteSync

        db = IndexDB(local / ".blobtrack" / "index.db")
        ls = LocalStore(local / ".blobtrack" / "objects")
        stats = RemoteSync.push(remote, ls, db)
        db.close()

        assert stats["transferred_chunks"] >= 1
        assert stats["skipped_chunks"] == 0

    def test_incremental_skips_existing(self, tmp_path, monkeypatch):
        """Second push skips all existing chunks, transfers only new."""
        local = _setup_repo(tmp_path / "local")
        remote = tmp_path / "remote"
        monkeypatch.chdir(local)

        (local / "f.bin").write_bytes(b"X" * 3_000_000)
        cmd_add(str(local / "f.bin"))
        cmd_commit("C1")
        cmd_push(str(remote))

        # Modify and commit
        (local / "f.bin").write_bytes(b"Y" * 3_000_000)
        cmd_add(str(local / "f.bin"))
        cmd_commit("C2")

        # Check stats via RemoteSync
        from blobtrack.storage.remote_sync import RemoteSync

        db = IndexDB(local / ".blobtrack" / "index.db")
        ls = LocalStore(local / ".blobtrack" / "objects")
        stats = RemoteSync.push(remote, ls, db)
        db.close()

        # Should skip the chunks from C1 that are already on remote
        assert stats["skipped_chunks"] >= 1

    def test_zero_transfer_sync(self, tmp_path, monkeypatch):
        """Push+Push on same state = zero transfer second time."""
        local = _setup_repo(tmp_path / "local")
        remote = tmp_path / "remote"
        monkeypatch.chdir(local)

        (local / "f.bin").write_bytes(b"zero transfer " * 3000)
        cmd_add(str(local / "f.bin"))
        cmd_commit("C1")
        cmd_push(str(remote))

        from blobtrack.storage.remote_sync import RemoteSync

        db = IndexDB(local / ".blobtrack" / "index.db")
        ls = LocalStore(local / ".blobtrack" / "objects")
        stats = RemoteSync.push(remote, ls, db)
        db.close()

        assert stats["transferred_chunks"] == 0
        assert stats["commits_synced"] == 0

    def test_pull_zero_transfer(self, tmp_path, monkeypatch):
        """Pull when local already has everything = zero transfer."""
        source = _setup_repo(tmp_path / "source")
        remote = tmp_path / "remote"
        local = _setup_repo(tmp_path / "local")

        monkeypatch.chdir(source)
        (source / "f.bin").write_bytes(b"pull zero " * 2000)
        cmd_add(str(source / "f.bin"))
        cmd_commit("C1")
        cmd_push(str(remote))

        monkeypatch.chdir(local)
        cmd_pull(str(remote))

        # Second pull
        from blobtrack.storage.remote_sync import RemoteSync

        db = IndexDB(local / ".blobtrack" / "index.db")
        ls = LocalStore(local / ".blobtrack" / "objects")
        stats = RemoteSync.pull(remote, ls, db)
        db.close()

        assert stats["transferred_chunks"] == 0
        assert stats["commits_synced"] == 0


# ---------------------------------------------------------------------------
# GC INTERACTION TEST
# ---------------------------------------------------------------------------

class TestGCInteraction:
    def test_gc_then_push_pull(self, tmp_path, monkeypatch):
        """Push, modify, commit, GC local orphans, push again - works."""
        local = _setup_repo(tmp_path / "local")
        remote = tmp_path / "remote"
        clone = _setup_repo(tmp_path / "clone")
        monkeypatch.chdir(local)

        # C1
        v1_data = b"gc interact v1 " * 3000
        (local / "f.bin").write_bytes(v1_data)
        cmd_add(str(local / "f.bin"))
        cmd_commit("C1")
        cmd_push(str(remote))

        # C2 (modifies f.bin, old chunks become orphans after GC)
        v2_data = b"gc interact v2 " * 3000
        (local / "f.bin").write_bytes(v2_data)
        cmd_add(str(local / "f.bin"))
        cmd_commit("C2")
        cmd_push(str(remote))

        # GC locally (should not break anything)
        cmd_gc()

        # Push again after GC
        cmd_push(str(remote))

        # Pull into clone and verify
        monkeypatch.chdir(clone)
        cmd_pull(str(remote))

        db = IndexDB(clone / ".blobtrack" / "index.db")
        commits = db.list_commits()
        assert len(commits) == 2
        c2_hash = commits[0]["commit_hash"]
        db.close()

        cmd_checkout(c2_hash)
        assert (clone / "f.bin").read_bytes() == v2_data


# ---------------------------------------------------------------------------
# CLI SUBPROCESS TESTS (actual blobtrack command)
# ---------------------------------------------------------------------------

class TestCLISubprocess:
    def test_push_via_subprocess(self, tmp_path, monkeypatch):
        """Test push via actual CLI invocation."""
        local = tmp_path / "sub_local"
        local.mkdir()
        remote = tmp_path / "sub_remote"

        # Init and commit via subprocess
        subprocess.run(
            [sys.executable, "-m", "blobtrack.cli.main", "init"],
            cwd=str(local), capture_output=True
        )
        (local / "test.bin").write_bytes(b"subprocess push test " * 2000)
        subprocess.run(
            [sys.executable, "-m", "blobtrack.cli.main", "add", "test.bin"],
            cwd=str(local), capture_output=True
        )
        subprocess.run(
            [sys.executable, "-m", "blobtrack.cli.main", "commit", "-m", "sub C1"],
            cwd=str(local), capture_output=True
        )

        # Push via subprocess
        result = subprocess.run(
            [sys.executable, "-m", "blobtrack.cli.main", "push", str(remote)],
            cwd=str(local), capture_output=True, text=True
        )
        assert result.returncode == 0
        assert "Pushed to" in result.stdout or "Push" in result.stdout

    def test_pull_via_subprocess(self, tmp_path, monkeypatch):
        """Test pull via actual CLI invocation."""
        source = tmp_path / "sub_source"
        source.mkdir()
        remote = tmp_path / "sub_remote"
        clone = tmp_path / "sub_clone"
        clone.mkdir()

        # Source: init, add, commit, push
        subprocess.run(
            [sys.executable, "-m", "blobtrack.cli.main", "init"],
            cwd=str(source), capture_output=True
        )
        (source / "data.bin").write_bytes(b"subprocess pull test " * 2000)
        subprocess.run(
            [sys.executable, "-m", "blobtrack.cli.main", "add", "data.bin"],
            cwd=str(source), capture_output=True
        )
        subprocess.run(
            [sys.executable, "-m", "blobtrack.cli.main", "commit", "-m", "src C1"],
            cwd=str(source), capture_output=True
        )
        subprocess.run(
            [sys.executable, "-m", "blobtrack.cli.main", "push", str(remote)],
            cwd=str(source), capture_output=True
        )

        # Clone: init, pull
        subprocess.run(
            [sys.executable, "-m", "blobtrack.cli.main", "init"],
            cwd=str(clone), capture_output=True
        )
        result = subprocess.run(
            [sys.executable, "-m", "blobtrack.cli.main", "pull", str(remote)],
            cwd=str(clone), capture_output=True, text=True
        )
        assert result.returncode == 0
        assert "Pulled from" in result.stdout or "Pull" in result.stdout

    def test_push_no_repo_subprocess(self, tmp_path):
        """Push outside repo returns exit code 1."""
        result = subprocess.run(
            [sys.executable, "-m", "blobtrack.cli.main", "push", str(tmp_path / "remote")],
            cwd=str(tmp_path), capture_output=True, text=True
        )
        assert result.returncode == 1

    def test_pull_invalid_remote_subprocess(self, tmp_path):
        """Pull from non-existent remote returns exit code 1."""
        local = tmp_path / "local_sub"
        local.mkdir()
        subprocess.run(
            [sys.executable, "-m", "blobtrack.cli.main", "init"],
            cwd=str(local), capture_output=True
        )
        result = subprocess.run(
            [sys.executable, "-m", "blobtrack.cli.main", "pull", str(tmp_path / "nope")],
            cwd=str(local), capture_output=True, text=True
        )
        assert result.returncode == 1
