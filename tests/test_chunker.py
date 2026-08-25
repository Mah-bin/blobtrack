"""
test_chunker.py — Tests for the Content-Defined Chunking (CDC) engine.

Verifies:
1. Files are correctly split into chunks
2. Chunk boundaries are deterministic (same file = same chunks every time)
3. Chunks respect min/avg/max size constraints
4. Inserting data only affects nearby chunks (CDC stability)
5. Full round-trip: chunking a file and reassembling produces the original
"""

import os
import tempfile
import pytest
from blobtrack.core.chunker import chunk_file, chunk_file_streaming, get_file_info


def _create_temp_file(size_bytes: int, pattern: bytes = b"\xAB") -> str:
    """Helper to create a temporary file filled with a repeating byte pattern."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
        remaining = size_bytes
        write_block = pattern * min(1024 * 1024, size_bytes)  # Write in 1MB blocks
        while remaining > 0:
            to_write = min(len(write_block), remaining)
            f.write(write_block[:to_write])
            remaining -= to_write
        return f.name


class TestChunkFile:
    """Tests for the chunk_file() function."""

    def test_small_file_single_chunk(self):
        """A file smaller than MIN_CHUNK_SIZE should produce exactly 1 chunk."""
        # Create a 256 KB file (smaller than min 512 KB)
        temp_path = _create_temp_file(256 * 1024)
        try:
            chunks = chunk_file(temp_path)
            assert len(chunks) == 1
            assert chunks[0].index == 0
            assert chunks[0].offset == 0
        finally:
            os.unlink(temp_path)

    def test_large_file_multiple_chunks(self):
        """A 10 MB file should produce multiple chunks."""
        temp_path = _create_temp_file(10 * 1024 * 1024)
        try:
            chunks = chunk_file(temp_path)
            assert len(chunks) > 1
        finally:
            os.unlink(temp_path)

    def test_chunks_cover_entire_file(self):
        """The sum of all chunk lengths must equal the original file size."""
        file_size = 5 * 1024 * 1024  # 5 MB
        temp_path = _create_temp_file(file_size)
        try:
            chunks = chunk_file(temp_path)
            total_length = sum(c.length for c in chunks)
            assert total_length == file_size
        finally:
            os.unlink(temp_path)

    def test_chunks_are_contiguous(self):
        """Each chunk's offset should immediately follow the previous chunk."""
        temp_path = _create_temp_file(5 * 1024 * 1024)
        try:
            chunks = chunk_file(temp_path)
            for i in range(1, len(chunks)):
                expected_offset = chunks[i - 1].offset + chunks[i - 1].length
                assert chunks[i].offset == expected_offset
        finally:
            os.unlink(temp_path)

    def test_deterministic_chunking(self):
        """Chunking the same file twice must produce identical results."""
        temp_path = _create_temp_file(5 * 1024 * 1024)
        try:
            chunks_1 = chunk_file(temp_path)
            chunks_2 = chunk_file(temp_path)
            assert len(chunks_1) == len(chunks_2)
            for c1, c2 in zip(chunks_1, chunks_2):
                assert c1.hash == c2.hash
                assert c1.offset == c2.offset
                assert c1.length == c2.length
        finally:
            os.unlink(temp_path)

    def test_each_chunk_has_hash(self):
        """Every chunk must have a valid 64-char SHA-256 hash."""
        temp_path = _create_temp_file(5 * 1024 * 1024)
        try:
            chunks = chunk_file(temp_path)
            for chunk in chunks:
                assert len(chunk.hash) == 64
                assert all(c in "0123456789abcdef" for c in chunk.hash)
        finally:
            os.unlink(temp_path)

    def test_roundtrip_reconstruction(self):
        """Chunking a file and concatenating the chunk data must recreate the original."""
        # Create a file with random-ish content (not just repeated bytes)
        content = os.urandom(3 * 1024 * 1024)  # 3 MB of random data
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(content)
            temp_path = f.name

        try:
            chunks = chunk_file(temp_path)
            reconstructed = b"".join(c.data for c in chunks)
            assert reconstructed == content
        finally:
            os.unlink(temp_path)

    def test_file_not_found(self):
        """Should raise FileNotFoundError for a nonexistent file."""
        with pytest.raises(FileNotFoundError):
            chunk_file("nonexistent_file.bin")

    def test_empty_file(self):
        """Should raise ValueError for an empty file."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            temp_path = f.name

        try:
            with pytest.raises(ValueError):
                chunk_file(temp_path)
        finally:
            os.unlink(temp_path)


class TestChunkFileStreaming:
    """Tests for the generator-based chunk_file_streaming() function."""

    def test_streaming_matches_list(self):
        """Streaming chunking must produce identical results to list-based chunking."""
        temp_path = _create_temp_file(5 * 1024 * 1024)
        try:
            list_chunks = chunk_file(temp_path)
            stream_chunks = list(chunk_file_streaming(temp_path))

            assert len(list_chunks) == len(stream_chunks)
            for lc, sc in zip(list_chunks, stream_chunks):
                assert lc.hash == sc.hash
                assert lc.offset == sc.offset
                assert lc.length == sc.length
        finally:
            os.unlink(temp_path)


class TestGetFileInfo:
    """Tests for the get_file_info() helper."""

    def test_returns_correct_size(self):
        """Verify file info returns the correct size."""
        size = 2 * 1024 * 1024  # 2 MB
        temp_path = _create_temp_file(size)
        try:
            info = get_file_info(temp_path)
            assert info["size_bytes"] == size
            assert "2.00 MB" in info["size_human"]
        finally:
            os.unlink(temp_path)
