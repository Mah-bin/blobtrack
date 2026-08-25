import os
import tempfile
import pytest
from blobtrack.core.chunker import chunk_file_streaming, read_chunk_at, get_file_info


def _create_temp_file(size_bytes: int, pattern: bytes = b"\xAB") -> str:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
        remaining = size_bytes
        write_block = pattern * min(1024 * 1024, size_bytes)
        while remaining > 0:
            to_write = min(len(write_block), remaining)
            f.write(write_block[:to_write])
            remaining -= to_write
        return f.name


class TestChunkFileStreaming:

    def test_small_file_single_chunk(self):
        temp_path = _create_temp_file(256 * 1024)
        try:
            chunks = list(chunk_file_streaming(temp_path))
            assert len(chunks) == 1
            assert chunks[0].index == 0
            assert chunks[0].offset == 0
        finally:
            os.unlink(temp_path)

    def test_large_file_multiple_chunks(self):
        temp_path = _create_temp_file(10 * 1024 * 1024)
        try:
            chunks = list(chunk_file_streaming(temp_path))
            assert len(chunks) > 1
        finally:
            os.unlink(temp_path)

    def test_chunks_cover_entire_file(self):
        file_size = 5 * 1024 * 1024
        temp_path = _create_temp_file(file_size)
        try:
            chunks = list(chunk_file_streaming(temp_path))
            total_length = sum(c.length for c in chunks)
            assert total_length == file_size
        finally:
            os.unlink(temp_path)

    def test_chunks_are_contiguous(self):
        temp_path = _create_temp_file(5 * 1024 * 1024)
        try:
            chunks = list(chunk_file_streaming(temp_path))
            for i in range(1, len(chunks)):
                expected_offset = chunks[i - 1].offset + chunks[i - 1].length
                assert chunks[i].offset == expected_offset
        finally:
            os.unlink(temp_path)

    def test_deterministic_chunking(self):
        temp_path = _create_temp_file(5 * 1024 * 1024)
        try:
            chunks_1 = list(chunk_file_streaming(temp_path))
            chunks_2 = list(chunk_file_streaming(temp_path))
            assert len(chunks_1) == len(chunks_2)
            for c1, c2 in zip(chunks_1, chunks_2):
                assert c1.offset == c2.offset
                assert c1.length == c2.length
                assert c1.data == c2.data
        finally:
            os.unlink(temp_path)

    def test_roundtrip_reconstruction(self):
        content = os.urandom(3 * 1024 * 1024)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(content)
            temp_path = f.name

        try:
            chunks = list(chunk_file_streaming(temp_path))
            reconstructed = b"".join(c.data for c in chunks)
            assert reconstructed == content
        finally:
            os.unlink(temp_path)

    def test_chunker_does_not_hash(self):
        temp_path = _create_temp_file(2 * 1024 * 1024)
        try:
            chunks = list(chunk_file_streaming(temp_path))
            for chunk in chunks:
                assert not hasattr(chunk, 'hash')
        finally:
            os.unlink(temp_path)

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            list(chunk_file_streaming("nonexistent_file.bin"))

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            temp_path = f.name

        try:
            with pytest.raises(ValueError):
                list(chunk_file_streaming(temp_path))
        finally:
            os.unlink(temp_path)


class TestReadChunkAt:

    def test_reads_correct_bytes(self):
        content = os.urandom(3 * 1024 * 1024)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(content)
            temp_path = f.name

        try:
            data = read_chunk_at(temp_path, offset=1024, length=2048)
            assert data == content[1024:1024 + 2048]
        finally:
            os.unlink(temp_path)


class TestGetFileInfo:

    def test_returns_correct_size(self):
        size = 2 * 1024 * 1024
        temp_path = _create_temp_file(size)
        try:
            info = get_file_info(temp_path)
            assert info["size_bytes"] == size
            assert "2.00 MB" in info["size_human"]
        finally:
            os.unlink(temp_path)
