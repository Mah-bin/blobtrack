"""
Local chunk object store implementation for blobtrack.
Stores and retrieves chunk blobs in .blobtrack/objects/<chunk_hash>.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple, Union


class LocalStore:
    """
    Manages physical storage of compressed chunk objects on the local filesystem.
    Uses atomic writes to guarantee chunk integrity.
    """

    def __init__(self, objects_dir: Union[str, Path]):
        self.objects_dir = Path(objects_dir)
        self.tmp_dir = self.objects_dir / ".tmp"
        self.init_store()

    def init_store(self) -> None:
        """Create objects and temp directories if they do not exist."""
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def get_chunk_path(self, chunk_hash: str) -> Path:
        """Get the absolute filesystem path for a chunk hash."""
        return self.objects_dir / chunk_hash

    def has_chunk(self, chunk_hash: str) -> bool:
        """
        Check if a chunk is already stored locally (O(1) deduplication check).
        """
        chunk_path = self.get_chunk_path(chunk_hash)
        return chunk_path.is_file()

    def store_chunk(self, chunk_hash: str, data: bytes) -> bool:
        """
        Store chunk data under .blobtrack/objects/<chunk_hash>.
        Uses atomic file replacement to prevent corrupt or partial writes.
        Returns True if chunk was newly written, False if already existed.
        """
        chunk_path = self.get_chunk_path(chunk_hash)
        if chunk_path.is_file():
            return False  # Already stored (deduplicated)

        # Write to temporary file in same filesystem, then atomic replace
        temp_file = tempfile.NamedTemporaryFile(
            dir=self.tmp_dir, delete=False, prefix="chunk_", suffix=".tmp"
        )
        try:
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_file.close()

            # Atomic replace / move
            shutil.move(temp_file.name, chunk_path)
            return True
        except Exception:
            if os.path.exists(temp_file.name):
                try:
                    os.remove(temp_file.name)
                except OSError:
                    pass
            raise

    def retrieve_chunk(self, chunk_hash: str) -> bytes:
        """
        Read raw chunk bytes from storage.
        Raises FileNotFoundError if chunk does not exist.
        """
        chunk_path = self.get_chunk_path(chunk_hash)
        if not chunk_path.is_file():
            raise FileNotFoundError(f"Chunk '{chunk_hash}' not found in local store at {chunk_path}")
        return chunk_path.read_bytes()

    def delete_chunk(self, chunk_hash: str) -> bool:
        """
        Delete a single chunk from the object store.
        Returns True if deleted, False if it was not found.
        """
        chunk_path = self.get_chunk_path(chunk_hash)
        if chunk_path.is_file():
            try:
                chunk_path.unlink()
                return True
            except OSError:
                return False
        return False

    def list_chunks(self) -> List[str]:
        """
        List all chunk hashes currently present in the object store.
        """
        if not self.objects_dir.is_dir():
            return []
        return [
            entry.name
            for entry in self.objects_dir.iterdir()
            if entry.is_file() and not entry.name.startswith(".")
        ]

    def get_chunk_size(self, chunk_hash: str) -> int:
        """
        Return the on-disk size (in bytes) of the stored chunk.
        """
        chunk_path = self.get_chunk_path(chunk_hash)
        if not chunk_path.is_file():
            raise FileNotFoundError(f"Chunk '{chunk_hash}' not found.")
        return chunk_path.stat().st_size

    def garbage_collect(self, active_hashes: Set[str]) -> Tuple[int, int]:
        """
        Scan all stored chunk files and delete those not present in active_hashes.
        Returns (deleted_chunks_count, total_freed_bytes).
        """
        deleted_count = 0
        freed_bytes = 0

        stored_hashes = self.list_chunks()
        for chk_hash in stored_hashes:
            if chk_hash not in active_hashes:
                chunk_path = self.get_chunk_path(chk_hash)
                try:
                    size = chunk_path.stat().st_size
                    chunk_path.unlink()
                    deleted_count += 1
                    freed_bytes += size
                except OSError:
                    continue

        return deleted_count, freed_bytes
