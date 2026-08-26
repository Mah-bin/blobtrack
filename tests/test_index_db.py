"""
Unit tests for IndexDB (SQLite metadata database).
"""

import time
import pytest
from pathlib import Path

from blobtrack.storage.index_db import IndexDB, init_db


@pytest.fixture
def temp_db(tmp_path: Path) -> IndexDB:
    db_path = tmp_path / ".blobtrack" / "index.db"
    db = IndexDB(db_path)
    yield db
    db.close()


def test_db_init(temp_db: IndexDB):
    assert temp_db.db_path.is_file()
    conn = temp_db._get_connection()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = {row[0] for row in cursor.fetchall()}
    assert {"files", "commits", "chunks", "chunk_refs"}.issubset(tables)


def test_standalone_init_db(tmp_path: Path):
    db_path = tmp_path / ".blobtrack" / "index.db"
    db = init_db(db_path)
    assert db.db_path.is_file()
    db.close()


def test_file_operations(temp_db: IndexDB):
    temp_db.register_file("data/video.mp4", "hash123", 1024000, 1700000000.0)
    file_info = temp_db.get_file("data/video.mp4")
    assert file_info is not None
    assert file_info["file_hash"] == "hash123"
    assert file_info["size"] == 1024000
    assert file_info["status"] == "tracked"

    temp_db.register_file("data/video.mp4", "hash456", 2048000, 1700000500.0)
    updated = temp_db.get_file("data/video.mp4")
    assert updated["file_hash"] == "hash456"
    assert updated["size"] == 2048000

    temp_db.register_file("model.bin", "modelhash", 5000, status="staged")
    all_files = temp_db.list_files()
    assert len(all_files) == 2

    staged_files = temp_db.list_files(status="staged")
    assert len(staged_files) == 1
    assert staged_files[0]["path"] == "model.bin"

    assert temp_db.remove_file("model.bin") is True
    assert temp_db.get_file("model.bin") is None
    assert len(temp_db.list_files()) == 1


def test_chunk_operations(temp_db: IndexDB):
    temp_db.record_chunk("chunk_aaa", 200, 120)
    chunk = temp_db.get_chunk("chunk_aaa")
    assert chunk is not None
    assert chunk["size_uncompressed"] == 200
    assert chunk["size_compressed"] == 120

    batch = [
        {"chunk_hash": "chunk_bbb", "size_uncompressed": 300, "size_compressed": 180},
        {"chunk_hash": "chunk_ccc", "size_uncompressed": 400, "size_compressed": 220},
    ]
    temp_db.record_chunks(batch)
    assert len(temp_db.list_chunks()) == 3


def test_commit_lifecycle_and_refs(temp_db: IndexDB):
    chunk_mappings = [
        {"file_path": "dataset.bin", "chunk_hash": "chk_1", "chunk_offset": 0, "chunk_length": 100, "chunk_order": 0},
        {"file_path": "dataset.bin", "chunk_hash": "chk_2", "chunk_offset": 100, "chunk_length": 150, "chunk_order": 1},
        {"file_path": "dataset.bin", "chunk_hash": "chk_3", "chunk_offset": 250, "chunk_length": 200, "chunk_order": 2},
    ]
    tree_data = {"root": "merkle_root_v1", "files": ["dataset.bin"]}

    commit_hash = "c0ffee1"
    temp_db.save_commit(
        commit_hash=commit_hash,
        message="Initial dataset commit",
        author="User <user@example.com>",
        timestamp=1700000000.0,
        merkle_root_hash="merkle_root_v1",
        tree_data=tree_data,
        file_chunk_mappings=chunk_mappings,
    )

    commit = temp_db.get_commit(commit_hash)
    assert commit is not None
    assert commit["message"] == "Initial dataset commit"
    assert commit["tree_data"] == tree_data

    refs = temp_db.get_commit_chunk_refs(commit_hash)
    assert len(refs) == 3

    file_chunks = temp_db.get_file_chunks_for_commit(commit_hash, "dataset.bin")
    assert len(file_chunks) == 3
    assert [fc["chunk_hash"] for fc in file_chunks] == ["chk_1", "chk_2", "chk_3"]
    assert [fc["chunk_order"] for fc in file_chunks] == [0, 1, 2]

    temp_db.save_commit(
        commit_hash="c0ffee2",
        parent_hash="c0ffee1",
        message="Second commit with modified chunk",
        timestamp=1700000100.0,
        merkle_root_hash="merkle_root_v2",
        file_chunk_mappings=[
            {"file_path": "dataset.bin", "chunk_hash": "chk_1", "chunk_order": 0},
            {"file_path": "dataset.bin", "chunk_hash": "chk_4", "chunk_order": 1},
            {"file_path": "dataset.bin", "chunk_hash": "chk_3", "chunk_order": 2},
        ],
    )

    latest = temp_db.get_latest_commit()
    assert latest["commit_hash"] == "c0ffee2"

    history = temp_db.list_commits()
    assert len(history) == 2
    assert history[0]["commit_hash"] == "c0ffee2"
    assert history[1]["commit_hash"] == "c0ffee1"


def test_orphan_and_active_chunks(temp_db: IndexDB):
    temp_db.record_chunk("orphan_chunk_99", 500, 300)

    temp_db.save_commit(
        commit_hash="c1",
        message="Commit 1",
        file_chunk_mappings=[
            {"file_path": "file1.dat", "chunk_hash": "active_chunk_1"},
            {"file_path": "file1.dat", "chunk_hash": "active_chunk_2"},
        ],
    )

    active = temp_db.get_active_chunk_hashes()
    assert active == {"active_chunk_1", "active_chunk_2"}

    orphans = temp_db.get_orphan_chunks()
    assert "orphan_chunk_99" in orphans
    assert "active_chunk_1" not in orphans

    deleted = temp_db.delete_chunk_records(["orphan_chunk_99"])
    assert deleted == 1
    assert temp_db.get_chunk("orphan_chunk_99") is None
