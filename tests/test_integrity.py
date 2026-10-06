"""Chunk integrity verification tests.

A chunk is named by the SHA-256 of its *uncompressed* bytes but stored
zstd-compressed, so verification has to decompress first. These tests pin that
behaviour down, including the failure modes.
"""

import hashlib

import pytest

from blobtrack.core.integrity import (
    ChunkIntegrityError,
    scan_chunks,
    verify_chunk_payload,
    verify_stored_chunk,
)
from blobtrack.core.packer import compress, decompress
from blobtrack.storage.local_store import LocalStore


@pytest.fixture
def store(tmp_path) -> LocalStore:
    return LocalStore(tmp_path / ".blobtrack" / "objects")


def _store_raw(store: LocalStore, raw: bytes) -> str:
    """Store a chunk the way the real pipeline does."""
    chunk_hash = hashlib.sha256(raw).hexdigest()
    store.store_chunk(chunk_hash, compress(raw))
    return chunk_hash


# ---------------------------------------------------------------------------
# verify_chunk_payload
# ---------------------------------------------------------------------------


def test_accepts_valid_payload():
    raw = b"the quick brown fox" * 100
    verify_chunk_payload(compress(raw), hashlib.sha256(raw).hexdigest())


def test_rejects_payload_that_does_not_match_its_hash():
    raw = b"original content"
    with pytest.raises(ChunkIntegrityError):
        verify_chunk_payload(compress(raw), hashlib.sha256(b"different").hexdigest())


def test_rejects_truncated_payload():
    raw = b"a" * 10000
    compressed = compress(raw)
    with pytest.raises(ChunkIntegrityError):
        verify_chunk_payload(compressed[: len(compressed) // 2], hashlib.sha256(raw).hexdigest())


def test_rejects_garbage_bytes():
    with pytest.raises(ChunkIntegrityError):
        verify_chunk_payload(b"not zstd at all", hashlib.sha256(b"x").hexdigest())


def test_error_message_names_the_chunk():
    raw = b"content"
    wrong = hashlib.sha256(b"other").hexdigest()
    with pytest.raises(ChunkIntegrityError) as exc:
        verify_chunk_payload(compress(raw), wrong)
    assert wrong[:12] in str(exc.value)


# ---------------------------------------------------------------------------
# verify_stored_chunk / LocalStore.verify_chunk
# ---------------------------------------------------------------------------


def test_verify_stored_chunk_accepts_good_chunk(store):
    chunk_hash = _store_raw(store, b"good content" * 500)
    verify_stored_chunk(store, chunk_hash)
    assert store.verify_chunk(chunk_hash) is True


def test_verify_chunk_reports_false_when_absent(store):
    assert store.verify_chunk(hashlib.sha256(b"never stored").hexdigest()) is False


def test_verify_chunk_detects_on_disk_corruption(store):
    """Simulate bit rot by rewriting the object with wrong content."""
    raw = b"valuable original data" * 400
    chunk_hash = _store_raw(store, raw)
    assert store.verify_chunk(chunk_hash) is True

    path = store.get_chunk_path(chunk_hash)
    path.write_bytes(compress(b"corrupted replacement"))

    assert store.verify_chunk(chunk_hash) is False


def test_retrieve_with_verify_raises_on_corruption(store):
    raw = b"data that will be damaged" * 200
    chunk_hash = _store_raw(store, raw)
    store.get_chunk_path(chunk_hash).write_bytes(compress(b"wrong"))

    # Default read still works (cheap path).
    assert store.retrieve_chunk(chunk_hash) is not None

    # Verified read refuses.
    with pytest.raises(ChunkIntegrityError):
        store.retrieve_chunk(chunk_hash, verify=True)


# ---------------------------------------------------------------------------
# scan_chunks
# ---------------------------------------------------------------------------


def test_scan_chunks_separates_missing_from_corrupt(store):
    good = _store_raw(store, b"good" * 1000)
    bad = _store_raw(store, b"bad content" * 1000)
    store.get_chunk_path(bad).write_bytes(compress(b"tampered"))
    absent = hashlib.sha256(b"never existed").hexdigest()

    missing, corrupt = scan_chunks(store, [good, bad, absent])

    assert missing == [absent]
    assert corrupt == [bad]


def test_scan_chunks_reports_nothing_for_healthy_store(store):
    hashes = [_store_raw(store, f"chunk {i}".encode() * 500) for i in range(5)]
    missing, corrupt = scan_chunks(store, hashes)
    assert missing == []
    assert corrupt == []


def test_roundtrip_is_byte_exact(store):
    raw = b"round trip payload " * 900
    chunk_hash = _store_raw(store, raw)
    assert decompress(store.retrieve_chunk(chunk_hash)) == raw
