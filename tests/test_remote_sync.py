"""
Unit and integration tests for RemoteSync (delta push and pull).
"""

import hashlib
from pathlib import Path

import pytest

from blobtrack.core.merkle_tree import build_tree, serialize_tree
from blobtrack.core.packer import compress, decompress
from blobtrack.storage.index_db import IndexDB
from blobtrack.storage.local_store import LocalStore
from blobtrack.storage.remote_sync import RemoteSync


def _put(local_store: LocalStore, raw: bytes) -> str:
    """Store a chunk the way the real pipeline does: zstd-compressed, named
    by the SHA-256 of the *uncompressed* bytes."""
    chunk_hash = hashlib.sha256(raw).hexdigest()
    local_store.store_chunk(chunk_hash, compress(raw))
    return chunk_hash


@pytest.fixture
def local_repo(tmp_path: Path):
    local_dir = tmp_path / "local"
    local_objects = local_dir / ".blobtrack" / "objects"
    local_db_path = local_dir / ".blobtrack" / "index.db"
    store = LocalStore(local_objects)
    db = IndexDB(local_db_path)
    yield store, db
    db.close()


@pytest.fixture
def remote_dir(tmp_path: Path) -> Path:
    return tmp_path / "remote"


def test_delta_push(local_repo, remote_dir: Path):
    """Push transfers only the chunks a commit needs, and only the new ones."""
    local_store, local_db = local_repo

    chunk_a = b"AAAA" * 100
    chunk_b = b"BBBB" * 100
    hash_a = _put(local_store, chunk_a)
    hash_b = _put(local_store, chunk_b)

    local_db.save_commit(
        commit_hash="commit_v1",
        message="Version 1",
        merkle_root_hash="root_v1",
        tree_data=serialize_tree(build_tree([hash_a, hash_b])),
        file_chunk_mappings=[
            {"file_path": "data.bin", "chunk_hash": hash_a, "chunk_order": 0},
            {"file_path": "data.bin", "chunk_hash": hash_b, "chunk_order": 1},
        ],
    )

    # First push has no common ancestor yet, so it is a full transfer.
    stats1 = RemoteSync.push(remote_dir, local_store, local_db)
    assert stats1["transferred_chunks"] == 2
    assert stats1["skipped_chunks"] == 0
    assert stats1["commits_synced"] == 1

    remote_store = LocalStore(remote_dir / ".blobtrack" / "objects")
    assert remote_store.has_chunk(hash_a) is True
    assert remote_store.has_chunk(hash_b) is True

    chunk_c = b"CCCC" * 100
    hash_c = _put(local_store, chunk_c)

    local_db.save_commit(
        commit_hash="commit_v2",
        parent_hash="commit_v1",
        message="Version 2 with new chunk",
        merkle_root_hash="root_v2",
        tree_data=serialize_tree(build_tree([hash_a, hash_c])),
        file_chunk_mappings=[
            {"file_path": "data.bin", "chunk_hash": hash_a, "chunk_order": 0},
            {"file_path": "data.bin", "chunk_hash": hash_c, "chunk_order": 1},
        ],
    )

    stats2 = RemoteSync.push(remote_dir, local_store, local_db)
    # commit_v1 is the common ancestor, so the Merkle delta between v1 and v2
    # is exactly {hash_c}. hash_a is shared and never crosses the wire, which
    # is why it does not even appear as a "skipped" chunk -- it was never a
    # candidate.
    assert stats2["used_merkle_delta"] is True
    assert stats2["transferred_chunks"] == 1
    assert stats2["skipped_chunks"] == 0
    assert stats2["commits_synced"] == 1

    remote_store = LocalStore(remote_dir / ".blobtrack" / "objects")
    assert remote_store.has_chunk(hash_c) is True


def test_delta_pull(local_repo, remote_dir: Path, tmp_path: Path):
    local_store, local_db = local_repo

    chunk_1 = b"DATA_1" * 50
    chunk_2 = b"DATA_2" * 50
    h1 = _put(local_store, chunk_1)
    h2 = _put(local_store, chunk_2)

    # Only h1 is referenced by the commit. h2 exists in the store but is an
    # orphan, and delta sync must not drag unreferenced chunks to the remote.
    local_db.save_commit(
        commit_hash="c_remote_1",
        message="Remote base commit",
        file_chunk_mappings=[{"file_path": "remote_file.bin", "chunk_hash": h1}],
    )

    push_stats = RemoteSync.push(remote_dir, local_store, local_db)
    assert push_stats["transferred_chunks"] == 1
    assert push_stats["commits_synced"] == 1

    remote_store = LocalStore(remote_dir / ".blobtrack" / "objects")
    assert remote_store.has_chunk(h1) is True
    assert remote_store.has_chunk(h2) is False, "orphan chunk must not be pushed"

    consumer_dir = tmp_path / "consumer"
    consumer_store = LocalStore(consumer_dir / ".blobtrack" / "objects")
    consumer_db = IndexDB(consumer_dir / ".blobtrack" / "index.db")

    assert consumer_store.has_chunk(h1) is False

    pull_stats = RemoteSync.pull(remote_dir, consumer_store, consumer_db)
    assert pull_stats["transferred_chunks"] == 1
    assert pull_stats["skipped_chunks"] == 0
    assert pull_stats["commits_synced"] == 1

    assert consumer_store.has_chunk(h1) is True
    assert decompress(consumer_store.retrieve_chunk(h1)) == chunk_1

    c = consumer_db.get_commit("c_remote_1")
    assert c is not None
    assert c["message"] == "Remote base commit"

    consumer_db.close()
