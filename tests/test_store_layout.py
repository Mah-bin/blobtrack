"""Object-store layout tests: fan-out, legacy fallback, and migration.

The store must be able to read repositories written before the fan-out change,
and `migrate` must move those chunks without losing any.
"""

import hashlib

import pytest

from blobtrack.storage.local_store import LocalStore


@pytest.fixture
def store(tmp_path) -> LocalStore:
    return LocalStore(tmp_path / ".blobtrack" / "objects")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Canonical layout
# ---------------------------------------------------------------------------


def test_store_uses_fanout_layout(store):
    chunk_hash = _hash(b"fanout payload")
    store.store_chunk(chunk_hash, b"compressed bytes")

    expected = store.objects_dir / chunk_hash[:2] / chunk_hash
    assert expected.is_file()
    assert store.get_chunk_path(chunk_hash) == expected


def test_store_creates_bucket_directory(store):
    chunk_hash = _hash(b"bucket payload")
    store.store_chunk(chunk_hash, b"x")
    assert (store.objects_dir / chunk_hash[:2]).is_dir()


def test_many_chunks_spread_across_buckets(store):
    for i in range(300):
        store.store_chunk(_hash(f"payload {i}".encode()), b"data")
    listed = store.list_chunks()
    assert len(listed) == 300
    buckets = {p.name for p in store.objects_dir.iterdir() if p.is_dir()}
    assert len(buckets) > 1, "fan-out should distribute chunks across buckets"


# ---------------------------------------------------------------------------
# Legacy layout compatibility
# ---------------------------------------------------------------------------


def test_reads_legacy_flat_chunk(store):
    """A repo written before fan-out must stay readable."""
    raw = b"legacy content"
    chunk_hash = _hash(raw)
    legacy_path = store.objects_dir / chunk_hash
    legacy_path.write_bytes(b"legacy bytes")

    assert store.has_chunk(chunk_hash) is True
    assert store.retrieve_chunk(chunk_hash) == b"legacy bytes"
    assert chunk_hash in store.list_chunks()


def test_store_is_noop_when_legacy_copy_exists(store):
    """Re-storing over an existing legacy chunk must not duplicate bytes."""
    chunk_hash = _hash(b"legacy")
    (store.objects_dir / chunk_hash).write_bytes(b"original")

    assert store.store_chunk(chunk_hash, b"new bytes") is False
    assert (store.objects_dir / chunk_hash).read_bytes() == b"original"
    assert not store.get_chunk_path(chunk_hash).exists()


def test_list_chunks_merges_both_layouts(store):
    for i in range(3):
        store.store_chunk(_hash(f"fanout {i}".encode()), b"x")
    for i in range(2):
        legacy_hash = _hash(f"legacy {i}".encode())
        (store.objects_dir / legacy_hash).write_bytes(b"x")

    listed = store.list_chunks()
    assert len(listed) == 5


def test_delete_removes_legacy_chunk(store):
    chunk_hash = _hash(b"legacy to delete")
    (store.objects_dir / chunk_hash).write_bytes(b"x")

    assert store.delete_chunk(chunk_hash) is True
    assert store.has_chunk(chunk_hash) is False


def test_gc_collects_legacy_chunks(store):
    keep = _hash(b"keep me")
    drop = _hash(b"drop me")
    store.store_chunk(keep, b"x")
    (store.objects_dir / drop).write_bytes(b"x")

    deleted, _ = store.garbage_collect({keep})

    assert deleted == 1
    assert store.has_chunk(keep) is True
    assert store.has_chunk(drop) is False


def test_tmp_dir_is_never_listed_as_a_chunk(store):
    store.store_chunk(_hash(b"real"), b"x")
    (store.tmp_dir / "leftover.tmp").write_bytes(b"junk")
    assert store.list_chunks() == [_hash(b"real")]


# ---------------------------------------------------------------------------
# migrate_layout
# ---------------------------------------------------------------------------


def test_migrate_moves_legacy_chunks_into_fanout(store):
    hashes = []
    for i in range(5):
        chunk_hash = _hash(f"legacy {i}".encode())
        (store.objects_dir / chunk_hash).write_bytes(f"payload {i}".encode())
        hashes.append(chunk_hash)

    migrated, moved_bytes = store.migrate_layout()

    assert migrated == 5
    assert moved_bytes > 0
    for chunk_hash in hashes:
        assert store.get_chunk_path(chunk_hash).is_file()
        assert not (store.objects_dir / chunk_hash).exists()
        assert store.has_chunk(chunk_hash) is True
    # list_chunks order follows directory iteration order, not insertion order.
    assert sorted(store.list_chunks()) == sorted(hashes)


def test_migrate_preserves_payload_bytes(store):
    payload = b"exact bytes matter"
    chunk_hash = _hash(payload)
    (store.objects_dir / chunk_hash).write_bytes(payload)

    store.migrate_layout()

    assert store.retrieve_chunk(chunk_hash) == payload


def test_migrate_is_idempotent(store):
    store.store_chunk(_hash(b"already fanout"), b"x")
    legacy = _hash(b"legacy one")
    (store.objects_dir / legacy).write_bytes(b"x")

    first, _ = store.migrate_layout()
    second, _ = store.migrate_layout()

    assert first == 1
    assert second == 0


def test_migrate_drops_redundant_flat_duplicate(store):
    """If both copies exist the flat one is removed, not duplicated again."""
    payload = b"duplicated content"
    chunk_hash = _hash(payload)
    store.store_chunk(chunk_hash, payload)
    (store.objects_dir / chunk_hash).write_bytes(payload)

    migrated, _ = store.migrate_layout()

    assert migrated == 1
    assert store.retrieve_chunk(chunk_hash) == payload
    assert store.list_chunks() == [chunk_hash]


def test_migrate_keeps_mixed_layout_working(store):
    """After migrating a mixed repo everything is still readable."""
    fanout_hash = _hash(b"already new")
    store.store_chunk(fanout_hash, b"new bytes")

    legacy_hash = _hash(b"still old")
    (store.objects_dir / legacy_hash).write_bytes(b"old bytes")

    store.migrate_layout()

    assert store.retrieve_chunk(fanout_hash) == b"new bytes"
    assert store.retrieve_chunk(legacy_hash) == b"old bytes"
    assert len(store.list_chunks()) == 2


def test_migrate_on_empty_store_is_safe(store):
    assert store.migrate_layout() == (0, 0)
