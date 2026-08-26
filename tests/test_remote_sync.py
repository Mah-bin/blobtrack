"""
Unit and integration tests for RemoteSync (delta push and pull).
"""

import hashlib
import pytest
from pathlib import Path

from blobtrack.storage.index_db import IndexDB
from blobtrack.storage.local_store import LocalStore
from blobtrack.storage.remote_sync import RemoteSync


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
    local_store, local_db = local_repo

    chunk_a = b"AAAA" * 100
    chunk_b = b"BBBB" * 100
    hash_a = hashlib.sha256(chunk_a).hexdigest()
    hash_b = hashlib.sha256(chunk_b).hexdigest()

    local_store.store_chunk(hash_a, chunk_a)
    local_store.store_chunk(hash_b, chunk_b)

    local_db.save_commit(
        commit_hash="commit_v1",
        message="Version 1",
        file_chunk_mappings=[
            {"file_path": "data.bin", "chunk_hash": hash_a, "chunk_order": 0},
            {"file_path": "data.bin", "chunk_hash": hash_b, "chunk_order": 1},
        ],
    )

    stats1 = RemoteSync.push(remote_dir, local_store, local_db)
    assert stats1["transferred_chunks"] == 2
    assert stats1["skipped_chunks"] == 0
    assert stats1["commits_synced"] == 1

    remote_store = LocalStore(remote_dir / ".blobtrack" / "objects")
    assert remote_store.has_chunk(hash_a) is True
    assert remote_store.has_chunk(hash_b) is True

    chunk_c = b"CCCC" * 100
    hash_c = hashlib.sha256(chunk_c).hexdigest()
    local_store.store_chunk(hash_c, chunk_c)

    local_db.save_commit(
        commit_hash="commit_v2",
        parent_hash="commit_v1",
        message="Version 2 with new chunk",
        file_chunk_mappings=[
            {"file_path": "data.bin", "chunk_hash": hash_a, "chunk_order": 0},
            {"file_path": "data.bin", "chunk_hash": hash_c, "chunk_order": 1},
        ],
    )

    stats2 = RemoteSync.push(remote_dir, local_store, local_db)
    assert stats2["transferred_chunks"] == 1
    assert stats2["skipped_chunks"] == 2
    assert stats2["commits_synced"] == 1


def test_delta_pull(local_repo, remote_dir: Path, tmp_path: Path):
    local_store, local_db = local_repo

    chunk_1 = b"DATA_1" * 50
    chunk_2 = b"DATA_2" * 50
    h1 = hashlib.sha256(chunk_1).hexdigest()
    h2 = hashlib.sha256(chunk_2).hexdigest()

    local_store.store_chunk(h1, chunk_1)
    local_store.store_chunk(h2, chunk_2)
    local_db.save_commit(
        commit_hash="c_remote_1",
        message="Remote base commit",
        file_chunk_mappings=[{"file_path": "remote_file.bin", "chunk_hash": h1}],
    )

    RemoteSync.push(remote_dir, local_store, local_db)

    consumer_dir = tmp_path / "consumer"
    consumer_store = LocalStore(consumer_dir / ".blobtrack" / "objects")
    consumer_db = IndexDB(consumer_dir / ".blobtrack" / "index.db")

    assert consumer_store.has_chunk(h1) is False

    pull_stats = RemoteSync.pull(remote_dir, consumer_store, consumer_db)
    assert pull_stats["transferred_chunks"] == 2
    assert pull_stats["skipped_chunks"] == 0
    assert pull_stats["commits_synced"] == 1

    assert consumer_store.has_chunk(h1) is True
    assert consumer_store.retrieve_chunk(h1) == chunk_1

    c = consumer_db.get_commit("c_remote_1")
    assert c is not None
    assert c["message"] == "Remote base commit"

    consumer_db.close()
