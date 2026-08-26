"""
Unit tests for LocalStore (chunk object store and local garbage collection).
"""

import hashlib
import os
import pytest
from pathlib import Path

from blobtrack.storage.local_store import LocalStore


@pytest.fixture
def temp_store(tmp_path: Path) -> LocalStore:
    objects_dir = tmp_path / ".blobtrack" / "objects"
    return LocalStore(objects_dir)


def test_init_store(temp_store: LocalStore):
    assert temp_store.objects_dir.is_dir()
    assert temp_store.tmp_dir.is_dir()


def test_store_and_retrieve_chunk(temp_store: LocalStore):
    data = b"hello world binary chunk data"
    chunk_hash = hashlib.sha256(data).hexdigest()

    assert temp_store.has_chunk(chunk_hash) is False
    assert temp_store.store_chunk(chunk_hash, data) is True
    assert temp_store.has_chunk(chunk_hash) is True

    assert temp_store.store_chunk(chunk_hash, data) is False

    retrieved = temp_store.retrieve_chunk(chunk_hash)
    assert retrieved == data
    assert temp_store.get_chunk_size(chunk_hash) == len(data)


def test_retrieve_missing_chunk(temp_store: LocalStore):
    with pytest.raises(FileNotFoundError):
        temp_store.retrieve_chunk("nonexistent_hash_12345")


def test_list_and_delete_chunks(temp_store: LocalStore):
    chunks = {
        hashlib.sha256(f"chunk_{i}".encode()).hexdigest(): f"chunk_{i}".encode()
        for i in range(5)
    }

    for h, data in chunks.items():
        temp_store.store_chunk(h, data)

    stored_list = temp_store.list_chunks()
    assert set(stored_list) == set(chunks.keys())

    target = list(chunks.keys())[0]
    assert temp_store.delete_chunk(target) is True
    assert temp_store.has_chunk(target) is False
    assert temp_store.delete_chunk(target) is False
    assert len(temp_store.list_chunks()) == 4


def test_garbage_collect(temp_store: LocalStore):
    data_map = {}
    for i in range(10):
        data = f"data_block_{i}".encode() * 100
        h = hashlib.sha256(data).hexdigest()
        temp_store.store_chunk(h, data)
        data_map[h] = len(data)

    all_hashes = list(data_map.keys())
    active_hashes = set(all_hashes[:4])
    orphan_hashes = set(all_hashes[4:])

    expected_freed_bytes = sum(data_map[h] for h in orphan_hashes)

    deleted_count, freed_bytes = temp_store.garbage_collect(active_hashes)
    assert deleted_count == 6
    assert freed_bytes == expected_freed_bytes

    for h in active_hashes:
        assert temp_store.has_chunk(h) is True

    for h in orphan_hashes:
        assert temp_store.has_chunk(h) is False
