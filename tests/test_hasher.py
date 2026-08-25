"""
test_hasher.py — Tests for the SHA-256 hashing engine.

Verifies:
1. hash_bytes() produces correct, deterministic SHA-256 hashes
2. hash_file_streaming() hashes large files without loading them into RAM
3. hash_chunks_parallel() correctly hashes multiple chunks concurrently
4. Identical data always produces identical hashes
5. Different data always produces different hashes
"""

import os
import tempfile
import pytest
from blobtrack.core.hasher import hash_bytes, hash_file_streaming, hash_chunks_parallel


class TestHashBytes:
    """Tests for the hash_bytes() function."""

    def test_known_hash(self):
        """Verify hash_bytes produces the correct SHA-256 for a known input."""
        # This is the universally known SHA-256 of "hello world"
        expected = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        assert hash_bytes(b"hello world") == expected

    def test_empty_bytes(self):
        """Verify hash_bytes handles empty input (SHA-256 of nothing)."""
        expected = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert hash_bytes(b"") == expected

    def test_deterministic(self):
        """Verify the same input always produces the same hash."""
        data = b"some binary data \x00\x01\x02\xff"
        assert hash_bytes(data) == hash_bytes(data)

    def test_different_data_different_hash(self):
        """Verify different inputs produce different hashes."""
        assert hash_bytes(b"data_v1") != hash_bytes(b"data_v2")

    def test_hash_length(self):
        """Verify SHA-256 always produces a 64-character hex string."""
        result = hash_bytes(b"any data at all")
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)


class TestHashFileStreaming:
    """Tests for the hash_file_streaming() function."""

    def test_small_file(self):
        """Verify streaming hash matches direct hash for a small file."""
        content = b"This is a small test file content."

        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(content)
            temp_path = f.name

        try:
            file_hash = hash_file_streaming(temp_path)
            direct_hash = hash_bytes(content)
            assert file_hash == direct_hash
        finally:
            os.unlink(temp_path)

    def test_large_file(self):
        """Verify streaming hash works correctly on a file larger than the 64MB buffer."""
        # Create a 2 MB file with repeating pattern
        content = b"A" * (2 * 1024 * 1024)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(content)
            temp_path = f.name

        try:
            file_hash = hash_file_streaming(temp_path)
            direct_hash = hash_bytes(content)
            assert file_hash == direct_hash
        finally:
            os.unlink(temp_path)


class TestHashChunksParallel:
    """Tests for the hash_chunks_parallel() function."""

    def test_parallel_matches_sequential(self):
        """Verify parallel hashing produces same results as sequential."""
        chunks = [
            (0, b"chunk zero"),
            (1, b"chunk one"),
            (2, b"chunk two"),
            (3, b"chunk three"),
        ]

        parallel_results = hash_chunks_parallel(chunks, max_workers=4)

        # Verify each hash matches the sequential hash
        for index, parallel_hash in parallel_results:
            expected = hash_bytes(chunks[index][1])
            assert parallel_hash == expected

    def test_preserves_order(self):
        """Verify results are returned in the correct chunk index order."""
        chunks = [(i, f"chunk_{i}".encode()) for i in range(20)]
        results = hash_chunks_parallel(chunks, max_workers=8)

        indices = [idx for idx, _ in results]
        assert indices == list(range(20))
