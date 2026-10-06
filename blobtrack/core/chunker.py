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

These three values determine every chunk boundary, and therefore every chunk
hash, in a repository. Changing them makes existing repositories unreadable,
which is why they are recorded in ``repo_meta`` and checked on every open.

Backends
--------
The rolling hash is a sequential prefix recurrence: byte *n*'s hash depends on
bytes *n-1, n-2, ...*. That cannot be vectorized, so the only way to make it
fast is to run it in compiled code. Three backends, tried in order:

``numba``
    JIT-compiled from the same algorithm. Requires the optional ``speed``
    extra (``pip install blobtrack[speed]``). Fastest to get working, and
    produces byte-identical boundaries.
``cython``
    The compiled accelerator that ships with fastcdc on interpreters where its
    wheel is available.
``python``
    fastcdc's own pure-Python implementation. Always available; correct but
    roughly two orders of magnitude slower.

All three must produce identical chunk boundaries, and
``tests/test_chunker.py`` asserts that they do. The backend is exposed as
:data:`CDC_BACKEND` so the CLI can report it and warn when it is slow.
"""

import contextlib
import io
import math
import os
from collections.abc import Iterator
from dataclasses import dataclass

# fastcdc prints "Running in pure python mode (slow)" to stdout at import time
# when its compiled accelerator is unavailable. That message is misleading
# once we have a faster backend of our own, and it pollutes the output of
# commands that never chunk anything, so it is suppressed here and replaced by
# our own report (see backend_description / the CLI warning).
with contextlib.redirect_stdout(io.StringIO()):
    from fastcdc import fastcdc

MIN_CHUNK_SIZE = 512 * 1024
AVG_CHUNK_SIZE = 2 * 1024 * 1024
MAX_CHUNK_SIZE = 8 * 1024 * 1024

# Identifies the chunking algorithm and its parameters. Bump only for an
# intentional format change, and expect existing repositories to stop opening.
CHUNKER_ID = "gear-cdc-v1"


@dataclass
class ChunkData:
    """One chunk: where it lives in the file plus its raw bytes."""

    index: int
    offset: int
    length: int
    data: bytes

    def __repr__(self) -> str:
        return (
            f"ChunkData(index={self.index}, offset={self.offset}, "
            f"length={self.length})"
        )


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

_NUMBA_OFFSET_FUNC = None
_NUMBA_CACHE: dict = {}


def _detect_cython_backend() -> bool:
    """True if fastcdc's compiled accelerator is the one bound as ``fastcdc``."""
    try:
        from fastcdc import fastcdc_cy  # noqa: F401

        return fastcdc.fastcdc is fastcdc_cy.fastcdc_cy
    except Exception:
        return False


def _try_numba():
    """Return a compiled ``cdc_offset``, or None if numba is unavailable.

    The numba implementation is a direct transliteration of fastcdc's pure
    Python ``cdc_offset`` below, so boundaries are identical. It is guarded
    defensively: any failure at import or compile time returns None and we
    fall back rather than break the caller.
    """
    global _NUMBA_OFFSET_FUNC
    if _NUMBA_OFFSET_FUNC is not None:
        return _NUMBA_OFFSET_FUNC

    try:
        import numpy as np
        from numba import njit

        gear = np.empty(256, dtype=np.uint64)
        gear[:] = np.fromiter(
            __import__("fastcdc.fastcdc_py", fromlist=["GEAR"]).GEAR,
            dtype=np.uint64,
            count=256,
        )

        @njit(cache=True)
        def _offset(data, mi, ma, cs, mask_s, mask_l):
            pattern = np.uint64(0)
            size = data.shape[0]
            i = min(mi, size)
            barrier = min(cs, size)
            while i < barrier:
                pattern = (pattern >> np.uint64(1)) + gear[data[i]]
                if not (pattern & mask_s):
                    return i + 1
                i += 1
            barrier = min(ma, size)
            while i < barrier:
                pattern = (pattern >> np.uint64(1)) + gear[data[i]]
                if not (pattern & mask_l):
                    return i + 1
                i += 1
            return i

        # Compile once with representative arguments; the result is cached on
        # disk by numba so subsequent processes skip this step.
        _offset(np.zeros(4096, dtype=np.uint8), 512, 2048, 1024, 0, 0)

        _NUMBA_OFFSET_FUNC = _offset
        return _offset
    except Exception:
        return None


def _select_backend() -> tuple:
    """Pick the fastest available backend. Returns (name, cdc_offset_callable)."""
    if os.environ.get("BLOBTRACK_DISABLE_NUMBA"):
        pass
    else:
        numba_offset = _try_numba()
        if numba_offset is not None:
            return "numba", numba_offset

    if _detect_cython_backend():
        return "cython", None  # use fastcdc's own generator directly

    return "python", None


CDC_BACKEND, _NUMBA_OFFSET_FUNC = _select_backend()
CDC_IS_NATIVE = CDC_BACKEND == "cython"


def backend_description() -> str:
    """Human-readable backend status, for `blob --version` and warnings."""
    if CDC_BACKEND == "numba":
        return "numba (JIT compiled)"
    if CDC_BACKEND == "cython":
        return "fastcdc (compiled accelerator)"
    return "pure Python (slow - install the 'speed' extra for a large speedup)"


def _ceil_div(x: int, y: int) -> int:
    return (x + y - 1) // y


def _center_size(average: int, minimum: int, source_size: int) -> int:
    """FastCDC's normalized-chunking center point."""
    offset = minimum + _ceil_div(minimum, 2)
    if offset > average:
        offset = average
    size = average - offset
    if size > source_size:
        return source_size
    return size


def _iter_cdc_chunks(
    filepath: str,
    min_size: int,
    avg_size: int,
    max_size: int,
) -> Iterator:
    """Yield (offset, length) pairs for a file using the active backend."""
    if _NUMBA_OFFSET_FUNC is None:
        # fastcdc handles both the compiled and pure-Python paths.
        for chunk in fastcdc(
            filepath, min_size=min_size, avg_size=avg_size, max_size=max_size
        ):
            yield chunk.offset, chunk.length
        return

    # numba path: same algorithm, compiled.
    import numpy as np

    with open(filepath, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        total = handle.tell()

    cs = _center_size(avg_size, min_size, max_size)
    bits = round(math.log2(avg_size))
    mask_s = 2 ** (bits + 1) - 1
    mask_l = 2 ** (bits - 1) - 1

    # Stream in windows so peak memory stays bounded regardless of file size.
    WINDOW = 64 * 1024 * 1024
    position = 0

    with open(filepath, "rb") as handle:
        while position < total:
            read_size = min(WINDOW, total - position)
            buffer = np.frombuffer(handle.read(read_size), dtype=np.uint8)
            if buffer.size == 0:
                break

            local = 0
            size = buffer.shape[0]
            while local < size:
                stop = min(local + max_size, size)
                cut = _NUMBA_OFFSET_FUNC(
                    buffer[local:stop], min_size, max_size, cs, mask_s, mask_l
                )
                if cut <= 0:
                    cut = stop - local
                yield position + local, cut
                local += cut

            position += size


def chunk_offsets(
    filepath: str,
    min_size: int = MIN_CHUNK_SIZE,
    avg_size: int = AVG_CHUNK_SIZE,
    max_size: int = MAX_CHUNK_SIZE,
) -> list:
    """Chunk boundaries only, as a list of ``(offset, length)``.

    Useful when the caller already has the file content in hand (as
    :func:`chunk_file_streaming` does) and only needs to know where the
    boundaries fall.
    """
    return list(_iter_cdc_chunks(filepath, min_size, avg_size, max_size))


def chunk_file_streaming(filepath: str) -> Iterator[ChunkData]:
    """Yield ChunkData for every chunk of ``filepath``, in file order.

    Keeps a single file handle open for the whole run and holds at most one
    chunk's worth of bytes in memory at a time, so a 500 GB file costs the
    same RAM as a 500 KB file.

    A file no larger than ``min_size`` yields exactly one chunk without
    invoking the rolling hash at all. That is not an approximation: when
    ``size <= min_size`` the scan loop returns immediately, so the result is
    byte-identical to running the full algorithm. It matters because repos of
    many small files would otherwise pay the full per-byte cost for nothing.

    Raises:
        FileNotFoundError: file does not exist.
        ValueError: file is empty (there is nothing to fingerprint).
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")

    file_size = os.path.getsize(filepath)
    if file_size == 0:
        raise ValueError(f"File is empty: {filepath}")

    if file_size <= MIN_CHUNK_SIZE:
        with open(filepath, "rb") as handle:
            yield ChunkData(index=0, offset=0, length=file_size, data=handle.read())
        return

    with open(filepath, "rb") as handle:
        for index, (offset, length) in enumerate(
            _iter_cdc_chunks(filepath, MIN_CHUNK_SIZE, AVG_CHUNK_SIZE, MAX_CHUNK_SIZE)
        ):
            handle.seek(offset)
            yield ChunkData(
                index=index,
                offset=offset,
                length=length,
                data=handle.read(length),
            )


def read_chunk_at(filepath: str, offset: int, length: int) -> bytes:
    """Read a specific byte range from a file on disk."""
    with open(filepath, "rb") as handle:
        handle.seek(offset)
        return handle.read(length)


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



