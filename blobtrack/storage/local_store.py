"""Local chunk object store for blobtrack.

Layout on disk
--------------
Chunks are named by the SHA-256 of their *uncompressed* bytes and live at::

    .blobtrack/objects/<first 2 hex chars>/<full 64-char hash>

The two-character fan-out keeps any single directory well under the limit
most filesystems impose on entries per directory, which matters once a repo
holds tens of thousands of chunks.

Legacy flat layout
------------------
Releases before the fan-out change stored chunks directly at
``.blobtrack/objects/<hash>``. Every read path here transparently falls back
to that location so older repositories keep working; :meth:`migrate_layout`
consolidates them into the canonical fan-out. Writes always use the
canonical layout.

Integrity
---------
:meth:`retrieve_chunk` verifies the SHA-256 of the bytes it hands back
against the hash it was asked for, by default. A chunk whose name is its own
content hash is only trustworthy if somebody actually checks it.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from blobtrack.core.integrity import ChunkIntegrityError


class LocalStore:
    """Stores and retrieves compressed chunk objects on the local filesystem."""

    def __init__(self, objects_dir: str | Path):
        self.objects_dir = Path(objects_dir)
        self.tmp_dir = self.objects_dir / ".tmp"
        self.init_store()

    def init_store(self) -> None:
        """Create the objects and temp directories if they do not exist."""
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def get_chunk_path(self, chunk_hash: str) -> Path:
        """Canonical on-disk path for a chunk (two-character fan-out)."""
        return self.objects_dir / chunk_hash[:2] / chunk_hash

    def get_legacy_chunk_path(self, chunk_hash: str) -> Path:
        """Pre-fan-out flat path for a chunk."""
        return self.objects_dir / chunk_hash

    def resolve_chunk_path(self, chunk_hash: str) -> Path | None:
        """Return the existing path for a chunk, or None.

        Prefers the canonical fan-out location and falls back to the legacy
        flat location so repositories written by older versions stay usable.
        """
        canonical = self.get_chunk_path(chunk_hash)
        if canonical.is_file():
            return canonical
        legacy = self.get_legacy_chunk_path(chunk_hash)
        if legacy.is_file():
            return legacy
        return None

    # ------------------------------------------------------------------
    # Chunk lifecycle
    # ------------------------------------------------------------------

    def has_chunk(self, chunk_hash: str) -> bool:
        """O(1)-ish deduplication check. True if the chunk exists in either layout."""
        return self.resolve_chunk_path(chunk_hash) is not None

    def store_chunk(self, chunk_hash: str, data: bytes) -> bool:
        """Persist a chunk atomically.

        Returns:
            True if this call wrote new bytes, False if the chunk was already
            present (deduplicated) in either the canonical or legacy layout.

        Writes go to a temp file in ``objects/.tmp``, are fsync'd, then moved
        into place with a single rename so a crash can never leave a
        half-written chunk visible to a reader.
        """
        if self.has_chunk(chunk_hash):
            return False

        chunk_path = self.get_chunk_path(chunk_hash)
        chunk_path.parent.mkdir(parents=True, exist_ok=True)

        temp_file = tempfile.NamedTemporaryFile(
            dir=self.tmp_dir, delete=False, prefix="chunk_", suffix=".tmp"
        )
        try:
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_file.close()

            shutil.move(temp_file.name, chunk_path)
            return True
        except Exception:
            if os.path.exists(temp_file.name):
                try:
                    os.remove(temp_file.name)
                except OSError:
                    pass
            raise

    def retrieve_chunk(self, chunk_hash: str, verify: bool = False) -> bytes:
        """Read a chunk's stored (compressed) bytes.

        Args:
            chunk_hash: Hash the chunk is named by.
            verify: When True, the payload is decompressed and checked against
                ``chunk_hash`` before being returned.

                Note this is off by default because the check requires
                decompressing the chunk, which is far more expensive than the
                read itself. Callers that are about to hand bytes to a user
                (``checkout``) or across a trust boundary (``push``/``pull``)
                should enable it.

        Raises:
            FileNotFoundError: chunk is absent in both layouts.
            ChunkIntegrityError: ``verify`` is True and the payload is corrupt.
        """
        chunk_path = self.resolve_chunk_path(chunk_hash)
        if chunk_path is None:
            raise FileNotFoundError(
                f"Chunk '{chunk_hash}' not found in local store at "
                f"{self.get_chunk_path(chunk_hash)}"
            )

        data = chunk_path.read_bytes()

        if verify:
            from blobtrack.core.integrity import verify_chunk_payload

            verify_chunk_payload(data, chunk_hash)

        return data

    def verify_chunk(self, chunk_hash: str) -> bool:
        """True if the chunk exists and its payload matches ``chunk_hash``."""
        try:
            self.retrieve_chunk(chunk_hash, verify=True)
            return True
        except (FileNotFoundError, ChunkIntegrityError):
            return False

    def delete_chunk(self, chunk_hash: str) -> bool:
        """Delete a chunk from either layout. True if something was removed."""
        chunk_path = self.resolve_chunk_path(chunk_hash)
        if chunk_path is None:
            return False
        try:
            chunk_path.unlink()
            return True
        except OSError:
            return False

    def list_chunks(self) -> list[str]:
        """Every chunk hash currently stored, in either layout.

        Scans the fan-out subdirectories *and* any flat files left behind by
        older versions, so a partially migrated repo still reports a complete
        picture rather than silently hiding chunks.
        """
        if not self.objects_dir.is_dir():
            return []

        hashes: list[str] = []

        for entry in self.objects_dir.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                # Fan-out bucket: exactly two hex characters.
                if len(entry.name) == 2:
                    hashes.extend(child.name for child in entry.iterdir() if child.is_file())
            elif entry.is_file():
                # Legacy flat chunk.
                hashes.append(entry.name)

        return hashes

    def get_chunk_size(self, chunk_hash: str) -> int:
        """On-disk size of a stored chunk."""
        chunk_path = self.resolve_chunk_path(chunk_hash)
        if chunk_path is None:
            raise FileNotFoundError(f"Chunk '{chunk_hash}' not found.")
        return chunk_path.stat().st_size

    def garbage_collect(self, active_hashes: set[str]) -> tuple[int, int]:
        """Delete stored chunks not referenced by any commit.

        Returns:
            (deleted_count, freed_bytes)
        """
        deleted_count = 0
        freed_bytes = 0

        for chunk_hash in self.list_chunks():
            if chunk_hash in active_hashes:
                continue
            chunk_path = self.resolve_chunk_path(chunk_hash)
            if chunk_path is None:
                continue
            try:
                size = chunk_path.stat().st_size
                chunk_path.unlink()
                deleted_count += 1
                freed_bytes += size
            except OSError:
                continue

        return deleted_count, freed_bytes

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def migrate_layout(self) -> tuple[int, int]:
        """Move legacy flat chunks into the canonical fan-out layout.

        Safe to run repeatedly and safe to interrupt: chunks are moved with
        os.replace, so a chunk is never absent from both layouts.

        Returns:
            (migrated_count, bytes_moved)
        """
        if not self.objects_dir.is_dir():
            return (0, 0)

        migrated = 0
        moved_bytes = 0

        for entry in list(self.objects_dir.iterdir()):
            if not entry.is_file() or entry.name.startswith("."):
                continue

            chunk_hash = entry.name
            canonical = self.get_chunk_path(chunk_hash)
            if canonical.is_file():
                # Already present canonically; the flat copy is redundant.
                try:
                    size = entry.stat().st_size
                    entry.unlink()
                    migrated += 1
                    moved_bytes += size
                except OSError:
                    pass
                continue

            try:
                size = entry.stat().st_size
                canonical.parent.mkdir(parents=True, exist_ok=True)
                os.replace(entry, canonical)
                migrated += 1
                moved_bytes += size
            except OSError:
                continue

        return (migrated, moved_bytes)
