"""Content-Defined Chunking (CDC) engine.

Slices a file into variable-sized chunks whose boundaries are decided by the
*data* (a rolling hash) rather than by position. That property is the whole
point: inserting one byte near the start of a 20 GB file only rewrites the
chunk boundaries around the insertion, so every other chunk keeps its
SHA-256 and stays deduplicated.

Chunk size limits
-----------------
min 512 KB  stops us producing millions of tiny chunks
avg 2 MB    the target for most chunks
max 8 MB    stops one chunk from ballooning

Backend note
------------
``fastcdc`` ships a Cython accelerator (``fastcdc_cy``). When it is not
importable the library silently falls back to a pure-Python implementation.
We detect which one is live and expose it as :data:`CDC_BACKEND` so the CLI
can warn instead of letting the user believe they are getting native speed.

Measured on a 24 MB file with Python 3.14 (no compiled accelerator available
for that interpreter):

    pure-Python chunking   ~5.6 s
    + SHA-256 of every chunk ~1.2 s
    + zstd level 3          ~0.1 s

So chunking dominates the pipeline, not compression. Two consequences worth
knowing:

* The ``needs_payload`` optimization in :func:`blobtrack.core.hasher.process_chunks`
  is still correct and still saves work, but on this interpreter it is not the
  bottleneck -- roughly 90% of the time is spent in the pure-Python rolling
  hash, which cannot be parallelized because it holds the GIL.
* Installing an interpreter that has a ``fastcdc`` wheel (CPython 3.10-3.13)
  is worth far more than any tuning inside this project.
"""

import os
from dataclasses import dataclass

from fastcdc import fastcdc

MIN_CHUNK_SIZE = 512 * 1024
AVG_CHUNK_SIZE = 2 * 1024 * 1024
MAX_CHUNK_SIZE = 8 * 1024 * 1024


def _detect_backend() -> str:
    """Return 'native' if the Cython CDC accelerator is active, else 'python'."""
    try:
        from fastcdc.fastcdc_cy import fastcdc_cy  # noqa: F401

        return "native"
    except Exception:
        return "python"


CDC_BACKEND = _detect_backend()
CDC_IS_NATIVE = CDC_BACKEND == "native"


@dataclass
class ChunkData:
    """One chunk: where it lives in the file plus its raw bytes."""

    index: int
    offset: int
    length: int
    data: bytes

    def __repr__(self) -> str:
        return f"ChunkData(index={self.index}, offset={self.offset}, length={self.length})"


def chunk_file_streaming(filepath: str):
    """Yield ChunkData for every chunk of ``filepath``, in file order.

    Keeps a single file handle open for the whole run and holds at most one
    chunk's worth of bytes in memory at a time, so a 500 GB file costs the
    same RAM as a 500 KB file.

    Raises:
        FileNotFoundError: file does not exist.
        ValueError: file is empty (there is nothing to fingerprint).
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")

    file_size = os.path.getsize(filepath)
    if file_size == 0:
        raise ValueError(f"File is empty: {filepath}")

    cdc_chunks = fastcdc(
        filepath,
        min_size=MIN_CHUNK_SIZE,
        avg_size=AVG_CHUNK_SIZE,
        max_size=MAX_CHUNK_SIZE,
    )

    with open(filepath, "rb") as f:
        for index, cdc_chunk in enumerate(cdc_chunks):
            f.seek(cdc_chunk.offset)
            raw_data = f.read(cdc_chunk.length)

            yield ChunkData(
                index=index,
                offset=cdc_chunk.offset,
                length=cdc_chunk.length,
                data=raw_data,
            )


def read_chunk_at(filepath: str, offset: int, length: int) -> bytes:
    """Read a specific byte range from a file on disk."""
    with open(filepath, "rb") as f:
        f.seek(offset)
        return f.read(length)


def get_file_info(filepath: str) -> dict:
    """Basic metadata about a file, before chunking."""
    file_size = os.path.getsize(filepath)
    return {
        "filename": os.path.basename(filepath),
        "filepath": os.path.abspath(filepath),
        "size_bytes": file_size,
        "size_human": _human_readable_size(file_size),
    }


def _human_readable_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"
