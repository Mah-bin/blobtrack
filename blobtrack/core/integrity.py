"""Content-integrity verification for stored chunks.

A blobtrack chunk is named by the SHA-256 of its **uncompressed** bytes, but
what lands on disk is the zstd-compressed form. So the filename cannot be
checked against the stored bytes directly -- verification has to decompress
first and hash the result.

Skipping that step means a truncated write, a bad sector or a mangled copy
would go undetected until a user tried to restore a file and got garbage. The
whole reason the store is content-addressed is so integrity is cheap to
check, so we check it on every read that matters.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from blobtrack.core.packer import decompress


class ChunkIntegrityError(Exception):
    """Raised when stored bytes do not decompress to the expected content."""


def verify_chunk_payload(compressed_data: bytes, expected_hash: str) -> None:
    """Decompress a chunk and assert it hashes to ``expected_hash``.

    Args:
        compressed_data: The stored (zstd-compressed) chunk bytes.
        expected_hash: The hash the chunk is named by, i.e. SHA-256 of the
            uncompressed content.

    Raises:
        ChunkIntegrityError: the payload is corrupt, truncated, or does not
            match the hash it is stored under.
    """
    try:
        raw = decompress(compressed_data)
    except Exception as exc:
        raise ChunkIntegrityError(
            f"chunk {expected_hash[:12]} could not be decompressed: {exc}"
        ) from exc

    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_hash:
        raise ChunkIntegrityError(
            f"chunk {expected_hash[:12]} failed integrity check: content hashes to {actual}"
        )


def verify_stored_chunk(store, chunk_hash: str) -> None:
    """Fetch a chunk from ``store`` and verify it. Raises on any problem."""
    data = store.retrieve_chunk(chunk_hash, verify=False)
    verify_chunk_payload(data, chunk_hash)


def scan_chunks(store, chunk_hashes: Iterable[str]) -> tuple[list, list]:
    """Verify many chunks, collecting failures instead of stopping at the first.

    Returns:
        (missing, corrupt) -- two lists of chunk hashes. Chunks that verify
        cleanly appear in neither list.
    """
    missing: list = []
    corrupt: list = []

    for chunk_hash in chunk_hashes:
        try:
            verify_stored_chunk(store, chunk_hash)
        except FileNotFoundError:
            missing.append(chunk_hash)
        except ChunkIntegrityError:
            corrupt.append(chunk_hash)
        except Exception:
            corrupt.append(chunk_hash)

    return missing, corrupt
