"""Repository format guard and chunk-boundary cache tests.

The format guard exists because chunk boundaries determine chunk hashes: a
repository written with different chunk sizes is not merely incompatible, it is
silently corrupt. These tests confirm the guard fires, and that pre-guard
repositories are adopted rather than rejected.
"""

from pathlib import Path

import pytest

from blobtrack.cli.commands import cmd_add, cmd_commit, cmd_fsck, cmd_init, cmd_log
from blobtrack.storage.index_db import (
    FormatMismatchError,
    IndexDB,
    chunker_signature,
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    base = tmp_path / "repo"
    base.mkdir()
    cmd_init(cwd=base)
    monkeypatch.chdir(base)
    return base


def _db(repo: Path) -> IndexDB:
    return IndexDB(repo / ".blobtrack" / "index.db")


# ---------------------------------------------------------------------------
# Signature recording
# ---------------------------------------------------------------------------

class TestFormatSignatureRecording:
    def test_init_records_the_signature(self, repo):
        with _db(repo) as db:
            signature = chunker_signature()
            assert db.get_meta("chunker_id") == signature["chunker_id"]
            assert int(db.get_meta("min_chunk_size")) == signature["min_chunk_size"]
            assert int(db.get_meta("avg_chunk_size")) == signature["avg_chunk_size"]
            assert int(db.get_meta("max_chunk_size")) == signature["max_chunk_size"]

    def test_init_records_a_format_version(self, repo):
        with _db(repo) as db:
            assert db.get_meta("format_version") == "1"

    def test_reopening_does_not_restamp(self, repo):
        """A hand-edited value must survive reopening; init only writes if absent."""
        with _db(repo) as db:
            db.set_meta("avg_chunk_size", "999999")

        with _db(repo) as db:
            assert db.get_meta("avg_chunk_size") == "999999"


class TestFormatGuardAccepts:
    def test_healthy_repo_passes(self, repo):
        with _db(repo) as db:
            db.verify_format_signature()  # must not raise

    def test_commands_work_on_healthy_repo(self, repo):
        (repo / "a.bin").write_bytes(b"a" * 5000)
        cmd_add("a.bin")
        cmd_commit("v1")
        cmd_log()
        cmd_fsck()

    def test_legacy_repo_without_signature_is_adopted(self, tmp_path, monkeypatch):
        """Repositories predating the guard must keep working, not be rejected."""
        base = tmp_path / "legacy"
        base.mkdir()
        cmd_init(cwd=base)
        monkeypatch.chdir(base)

        with _db(base) as db:
            # Simulate a repo written before the guard existed.
            conn = db._get_connection()
            with conn:
                conn.execute("DELETE FROM repo_meta;")
            assert db.get_meta("chunker_id") is None

        (base / "a.bin").write_bytes(b"a" * 5000)
        cmd_add("a.bin")
        cmd_commit("v1 in a legacy repo")

        with _db(base) as db:
            assert db.get_meta("chunker_id") is not None


class TestFormatGuardRejects:
    def test_mismatched_chunk_size_raises(self, repo):
        with _db(repo) as db:
            db.set_meta("avg_chunk_size", "999999")
            with pytest.raises(FormatMismatchError):
                db.verify_format_signature()

    def test_mismatched_chunker_id_raises(self, repo):
        with _db(repo) as db:
            db.set_meta("chunker_id", "some-other-chunker-v9")
            with pytest.raises(FormatMismatchError):
                db.verify_format_signature()

    def test_mismatch_message_names_the_problem(self, repo):
        with _db(repo) as db:
            db.set_meta("avg_chunk_size", "999999")
            with pytest.raises(FormatMismatchError) as exc:
                db.verify_format_signature()
            message = str(exc.value)
            assert "avg_chunk_size" in message
            assert "999999" in message

    def test_commands_refuse_a_mismatched_repo(self, repo, monkeypatch):
        """The guard must stop writes, not merely warn."""
        (repo / "a.bin").write_bytes(b"a" * 5000)
        cmd_add("a.bin")
        cmd_commit("v1")

        with _db(repo) as db:
            db.set_meta("max_chunk_size", "12345678")

        with pytest.raises(SystemExit) as exc:
            cmd_commit("should be refused")
        assert exc.value.code == 1

    def test_refused_commit_writes_nothing(self, repo):
        (repo / "a.bin").write_bytes(b"a" * 5000)
        cmd_add("a.bin")
        cmd_commit("v1")

        with _db(repo) as db:
            before = db.count_commits()
            db.set_meta("min_chunk_size", "7")
            db.close()

        with pytest.raises(SystemExit):
            cmd_commit("refused")

        with _db(repo) as db:
            assert db.count_commits() == before


# ---------------------------------------------------------------------------
# Chunk-boundary cache
# ---------------------------------------------------------------------------

class TestChunkCache:
    def test_commit_populates_the_cache(self, repo):
        (repo / "big.bin").write_bytes(b"payload data " * 400_000)
        cmd_add("big.bin")
        cmd_commit("v1")

        with _db(repo) as db:
            assert db.count_cached_chunks() >= 1

    def test_second_commit_reuses_the_cache(self, repo):
        (repo / "big.bin").write_bytes(b"payload data " * 400_000)
        cmd_add("big.bin")
        cmd_commit("v1")

        with _db(repo) as db:
            first = db.get_cached_chunks(
                _file_hash(repo, "big.bin")
            )

        cmd_commit("v2 unchanged")

        with _db(repo) as db:
            second = db.get_cached_chunks(_file_hash(repo, "big.bin"))

        assert first is not None
        assert second == first, "cache must return identical boundaries"

    def test_cache_returns_correct_order_and_offsets(self, repo):
        (repo / "big.bin").write_bytes(b"payload data " * 400_000)
        cmd_add("big.bin")
        cmd_commit("v1")

        with _db(repo) as db:
            entries = db.get_cached_chunks(_file_hash(repo, "big.bin"))

        assert entries is not None
        assert len(entries) >= 1
        running = 0
        for entry in entries:
            assert entry["offset"] == running
            assert len(entry["hash"]) == 64
            assert entry["length"] > 0
            running += entry["length"]
        assert running == (repo / "big.bin").stat().st_size

    def test_cache_reflects_an_edited_file(self, repo):
        (repo / "big.bin").write_bytes(b"payload data " * 400_000)
        cmd_add("big.bin")
        cmd_commit("v1")

        with _db(repo) as db:
            before = db.get_cached_chunks(_file_hash(repo, "big.bin"))

        handle = open(repo / "big.bin", "r+b")
        handle.seek(1_000_000)
        handle.write(b"Z" * 2000)
        handle.close()
        cmd_add("big.bin")
        cmd_commit("v2 edited")

        with _db(repo) as db:
            after = db.get_cached_chunks(_file_hash(repo, "big.bin"))

        assert after is not None
        assert after != before, "edited content must produce a different cache entry"

    def test_cache_survives_checkout(self, repo):
        """Restore a file from history and confirm its boundaries are unchanged."""
        (repo / "big.bin").write_bytes(b"payload data " * 400_000)
        cmd_add("big.bin")
        cmd_commit("v1")

        with _db(repo) as db:
            commit_hash = db.get_branch_head("main")
            original = db.get_cached_chunks(_file_hash(repo, "big.bin"))

        handle = open(repo / "big.bin", "r+b")
        handle.seek(500)
        handle.write(b"Q" * 100)
        handle.close()
        cmd_add("big.bin")
        cmd_commit("v2")

        from blobtrack.cli.commands import cmd_checkout

        cmd_checkout(commit_hash)

        with _db(repo) as db:
            restored = db.get_cached_chunks(_file_hash(repo, "big.bin"))

        assert restored == original, (
            "restored content must resolve to the same cached boundaries"
        )

    def test_clear_cache_is_safe_and_reversible(self, repo):
        (repo / "big.bin").write_bytes(b"payload data " * 400_000)
        cmd_add("big.bin")
        cmd_commit("v1")

        with _db(repo) as db:
            assert db.clear_chunk_cache() >= 1
            assert db.count_cached_chunks() == 0
            # Still works with an empty cache.
            cmd_commit("v2 after cache clear")
            assert db.count_cached_chunks() >= 1

    def test_malformed_cache_entry_is_ignored(self, repo):
        """A corrupt cache row must degrade to re-chunking, never crash."""
        (repo / "big.bin").write_bytes(b"payload data " * 400_000)
        cmd_add("big.bin")
        cmd_commit("v1")

        with _db(repo) as db:
            file_hash = _file_hash(repo, "big.bin")
            conn = db._get_connection()
            with conn:
                conn.execute(
                    "UPDATE chunk_cache SET chunk_hashes = ? WHERE file_hash = ?",
                    ("not json at all", file_hash),
                )

            assert db.get_cached_chunks(file_hash) is None
            cmd_commit("v2 despite corrupt cache")


def _file_hash(repo: Path, name: str) -> str:
    import hashlib

    return hashlib.sha256((repo / name).read_bytes()).hexdigest()
