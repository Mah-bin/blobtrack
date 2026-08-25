import os
import tempfile
import pytest
from blobtrack.core.hasher import hash_bytes, hash_file_streaming, process_chunks
from blobtrack.core.packer import decompress
from blobtrack.core.chunker import chunk_file_streaming


class TestHashBytes:

    def test_known_hash(self):
        expected = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        assert hash_bytes(b"hello world") == expected

    def test_empty_bytes(self):
        expected = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert hash_bytes(b"") == expected

    def test_deterministic(self):
        data = b"some binary data \x00\x01\x02\xff"
        assert hash_bytes(data) == hash_bytes(data)

    def test_different_data_different_hash(self):
        assert hash_bytes(b"data_v1") != hash_bytes(b"data_v2")

    def test_hash_length(self):
        result = hash_bytes(b"any data at all")
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)


class TestHashFileStreaming:

    def test_small_file(self):
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


class TestProcessChunks:

    def test_produces_hashes(self):
        content = os.urandom(3 * 1024 * 1024)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(content)
            temp_path = f.name

        try:
            stream = chunk_file_streaming(temp_path)
            results = list(process_chunks(stream, batch_size=4, max_workers=4))
            assert len(results) > 0
            for r in results:
                assert len(r.hash) == 64
                assert all(c in "0123456789abcdef" for c in r.hash)
        finally:
            os.unlink(temp_path)

    def test_preserves_strict_order(self):
        content = os.urandom(10 * 1024 * 1024)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(content)
            temp_path = f.name

        try:
            stream = chunk_file_streaming(temp_path)
            results = list(process_chunks(stream, batch_size=4, max_workers=8))
            indices = [r.index for r in results]
            assert indices == list(range(len(results)))
        finally:
            os.unlink(temp_path)

    def test_compressed_data_decompresses_correctly(self):
        content = os.urandom(3 * 1024 * 1024)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(content)
            temp_path = f.name

        try:
            raw_chunks = list(chunk_file_streaming(temp_path))
            stream = chunk_file_streaming(temp_path)
            processed = list(process_chunks(stream, batch_size=4, max_workers=4))

            assert len(raw_chunks) == len(processed)
            for raw, proc in zip(raw_chunks, processed):
                decompressed = decompress(proc.compressed_data)
                assert decompressed == raw.data
        finally:
            os.unlink(temp_path)

    def test_hash_matches_direct_hash(self):
        content = os.urandom(3 * 1024 * 1024)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(content)
            temp_path = f.name

        try:
            raw_chunks = list(chunk_file_streaming(temp_path))
            stream = chunk_file_streaming(temp_path)
            processed = list(process_chunks(stream, batch_size=4, max_workers=4))

            for raw, proc in zip(raw_chunks, processed):
                expected_hash = hash_bytes(raw.data)
                assert proc.hash == expected_hash
        finally:
            os.unlink(temp_path)

    def test_full_roundtrip_reconstruction(self):
        content = os.urandom(5 * 1024 * 1024)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(content)
            temp_path = f.name

        try:
            stream = chunk_file_streaming(temp_path)
            processed = list(process_chunks(stream, batch_size=4, max_workers=4))

            reconstructed = b"".join(
                decompress(p.compressed_data) for p in processed
            )
            assert reconstructed == content
        finally:
            os.unlink(temp_path)
