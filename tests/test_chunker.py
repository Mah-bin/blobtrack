"""Chunker correctness and performance-path tests.

The critical property: **every backend and every fast path must produce
byte-identical chunk boundaries.** Boundaries determine chunk hashes, so any
divergence would silently produce a repository that cannot be verified or
reconstructed. These tests pin that down rather than trusting it.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from blobtrack.core import chunker
from blobtrack.core.chunker import (
    AVG_CHUNK_SIZE,
    CHUNKER_ID,
    MAX_CHUNK_SIZE,
    MIN_CHUNK_SIZE,
    backend_description,
    chunk_file_streaming,
    chunk_offsets,
)


def _reference_offsets(path: Path) -> list:
    """Boundaries produced by fastcdc's own pure-Python implementation.

    This is the ground truth every other path must match.
    """
    from fastcdc.fastcdc_py import fastcdc_py

    return [
        (c.offset, c.length)
        for c in fastcdc_py(
            str(path), min_size=MIN_CHUNK_SIZE, avg_size=AVG_CHUNK_SIZE,
            max_size=MAX_CHUNK_SIZE,
        )
    ]


def _write(tmp_path: Path, size: int, seed: int = 0) -> Path:
    import random

    rng = random.Random(seed)
    path = tmp_path / f"f{size}_{seed}.bin"
    path.write_bytes(bytes(rng.getrandbits(8) for _ in range(min(size, 200_000)))
                     * (max(size // 200_000, 1)))
    # Guarantee exact size.
    data = path.read_bytes()
    if len(data) < size:
        data = data + bytes(size - len(data))
    path.write_bytes(data[:size])
    return path


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

class TestBackend:
    def test_backend_is_one_of_the_known_values(self):
        assert chunker.CDC_BACKEND in ("numba", "cython", "python")

    def test_backend_description_is_informative(self):
        description = backend_description()
        assert description
        assert isinstance(description, str)

    def test_disable_env_forces_python_backend(self):
        """The escape hatch must work, or the pure-python path is untestable."""
        code = (
            "import os; os.environ['BLOBTRACK_DISABLE_NUMBA']='1';"
            "from blobtrack.core import chunker; print(chunker.CDC_BACKEND)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert result.returncode == 0
        assert result.stdout.strip().endswith("python")

    def test_chunker_id_is_declared(self):
        assert CHUNKER_ID


# ---------------------------------------------------------------------------
# Byte-identity: the property that must never break
# ---------------------------------------------------------------------------

class TestBoundaryIdentity:
    @pytest.mark.parametrize(
        "size",
        [
            1,
            1024,
            MIN_CHUNK_SIZE - 1,
            MIN_CHUNK_SIZE,
            MIN_CHUNK_SIZE + 1,
            2 * 1024 * 1024,
            3 * 1024 * 1024,
            MAX_CHUNK_SIZE + 7,
            12_000_000,
        ],
    )
    def test_offsets_match_fastcdc_reference(self, tmp_path, size):
        path = _write(tmp_path, size, seed=size)
        assert chunk_offsets(str(path)) == _reference_offsets(path)

    def test_streaming_matches_offsets_api(self, tmp_path):
        path = _write(tmp_path, 5_000_000, seed=7)
        streamed = [(c.offset, c.length) for c in chunk_file_streaming(str(path))]
        assert streamed == chunk_offsets(str(path))
        assert streamed == _reference_offsets(path)

    def test_reconstruction_is_byte_exact(self, tmp_path):
        import random

        rng = random.Random(99)
        payload = bytes(rng.getrandbits(8) for _ in range(4_000_000))
        path = tmp_path / "rt.bin"
        path.write_bytes(payload)

        rebuilt = b"".join(c.data for c in chunk_file_streaming(str(path)))
        assert rebuilt == payload

    def test_chunks_are_contiguous_and_cover_the_file(self, tmp_path):
        path = _write(tmp_path, 6_000_000, seed=3)
        size = path.stat().st_size
        offsets = chunk_offsets(str(path))
        assert sum(length for _, length in offsets) == size
        running = 0
        for offset, length in offsets:
            assert offset == running
            running += length

    def test_chunk_sizes_respect_limits(self, tmp_path):
        path = _write(tmp_path, 12_000_000, seed=11)
        for _, length in chunk_offsets(str(path)):
            # Only the final chunk of a file may fall below the minimum.
            assert length <= MAX_CHUNK_SIZE
            assert length >= MIN_CHUNK_SIZE or length == path.stat().st_size - (
                chunk_offsets(str(path))[-1][0]
            )

    def test_chunking_is_deterministic(self, tmp_path):
        path = _write(tmp_path, 5_000_000, seed=13)
        assert chunk_offsets(str(path)) == chunk_offsets(str(path))


# ---------------------------------------------------------------------------
# Small-file fast path
# ---------------------------------------------------------------------------

class TestSmallFileFastPath:
    @pytest.mark.parametrize("size", [1, 100, 50_000, MIN_CHUNK_SIZE - 1, MIN_CHUNK_SIZE])
    def test_small_files_match_full_cdc_exactly(self, tmp_path, size):
        """The shortcut must be provably identical, not merely plausible."""
        path = _write(tmp_path, size, seed=size)
        streamed = [(c.offset, c.length) for c in chunk_file_streaming(str(path))]
        assert streamed == _reference_offsets(path)

    def test_small_file_yields_exactly_one_chunk(self, tmp_path):
        path = _write(tmp_path, 1000, seed=5)
        chunks = list(chunk_file_streaming(str(path)))
        assert len(chunks) == 1
        assert chunks[0].offset == 0
        assert chunks[0].length == 1000

    def test_small_file_content_is_preserved(self, tmp_path):
        payload = bytes(range(256)) * 4
        path = tmp_path / "s.bin"
        path.write_bytes(payload)
        assert list(chunk_file_streaming(str(path)))[0].data == payload

    def test_file_exactly_at_min_size(self, tmp_path):
        path = _write(tmp_path, MIN_CHUNK_SIZE, seed=17)
        streamed = [(c.offset, c.length) for c in chunk_file_streaming(str(path))]
        assert streamed == _reference_offsets(path)


# ---------------------------------------------------------------------------
# Errors still raise
# ---------------------------------------------------------------------------

class TestChunkerErrors:
    def test_missing_file(self):
        with pytest.raises(FileNotFoundError):
            list(chunk_file_streaming("no_such_file_here.bin"))

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as handle:
            empty = handle.name
        try:
            with pytest.raises(ValueError):
                list(chunk_file_streaming(empty))
        finally:
            os.unlink(empty)


# ---------------------------------------------------------------------------
# Format signature
# ---------------------------------------------------------------------------

class TestFormatSignature:
    def test_signature_reports_all_parameters(self):
        from blobtrack.storage.index_db import chunker_signature

        signature = chunker_signature()
        assert signature["chunker_id"] == CHUNKER_ID
        assert signature["min_chunk_size"] == MIN_CHUNK_SIZE
        assert signature["avg_chunk_size"] == AVG_CHUNK_SIZE
        assert signature["max_chunk_size"] == MAX_CHUNK_SIZE

    def test_signature_tracks_constant_changes(self, monkeypatch):
        """If a chunk size changes, the signature must change with it, so the
        guard can detect a mismatch instead of silently corrupting repos."""
        from blobtrack.storage import index_db

        monkeypatch.setattr(chunker, "AVG_CHUNK_SIZE", 4 * 1024 * 1024)
        changed = index_db.chunker_signature()
        assert changed["avg_chunk_size"] == 4 * 1024 * 1024
        assert changed["avg_chunk_size"] != AVG_CHUNK_SIZE
