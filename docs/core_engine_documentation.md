# Core Engine

The pipeline: **chunk → hash → compress → store**, and the two structures that
make incremental versioning possible (Merkle trees and content deltas).

---

## `core/chunker.py` — Content-Defined Chunking

### Why not fixed-size chunks

Fixed-size chunking is destroyed by a single insertion. Add one byte to the
front of a file and every boundary moves by one, so every chunk's contents
change, every hash changes, and deduplication saves nothing.

Content-defined chunking cuts where the *data* indicates a boundary, using a
rolling hash (FastCDC). Inserting bytes only shifts the boundaries near the
insertion; every other chunk keeps its exact contents and therefore its
SHA-256.

### Constants

```python
MIN_CHUNK_SIZE  = 512 * 1024    # 512 KB — avoids millions of tiny chunks
AVG_CHUNK_SIZE  =   2 * 1024 * 1024   # 2 MB — the target for most chunks
MAX_CHUNK_SIZE  =   8 * 1024 * 1024   # 8 MB — bounds any single chunk
```

### `class ChunkData`

```python
@dataclass
class ChunkData:
    index: int     # 0-based position in the file
    offset: int    # byte offset where this chunk starts
    length: int    # length in bytes
    data: bytes    # raw (uncompressed) chunk contents
```

### `chunk_file_streaming(filepath) -> Generator[ChunkData]`

Yields chunks in file order.

- Holds **one** file handle open for the entire run rather than reopening per
  chunk, which would cause disk thrashing on files with thousands of chunks.
- Yields lazily, so only one chunk of raw bytes is resident at a time. Peak
  memory is flat regardless of file size.
- Raises `FileNotFoundError` if the file is missing, `ValueError` if empty.

### `read_chunk_at(filepath, offset, length) -> bytes`

Random access to one byte range, used when reconstructing a file chunk by
chunk.

### `get_file_info(filepath) -> dict`

Filename, absolute path, size in bytes, and a human-readable size string.

### Backend detection

### Backends

The rolling hash is a sequential prefix recurrence: byte *n*'s hash depends on
bytes *n−1, n−2, …*. It cannot be vectorized or parallelized — the pure-Python
loop holds the GIL for its entire duration. The only way to make it fast is to
run it in compiled code.

```python
CDC_BACKEND      # "numba" | "cython" | "python"
CDC_IS_NATIVE    # True only for fastcdc's compiled accelerator
CHUNKER_ID       # "gear-cdc-v1" - identifies the format
backend_description()  # human-readable, shown by `blob --version`
```

Backends are tried in order:

| Backend | Selected when | Measured |
|---|---|---|
| `numba` | `pip install blobtrack[speed]` | ~4.7 s → ~0.28 s on 24 MB (**~17x**) |
| `cython` | fastcdc's `fastcdc_cy` is importable | fast |
| `python` | always available | baseline |

**`fastcdc` publishes no compiled wheels on PyPI for any version**, so its
pure-Python implementation runs by default on every interpreter. Earlier
versions of this project claimed wheels existed for CPython 3.10–3.13; that was
wrong, and it is why this section exists.

The `numba` backend is a direct transliteration of the same algorithm, so
**chunk boundaries are byte-identical**. `tests/test_chunker.py` asserts this
against fastcdc's reference implementation across many file sizes, including
the boundaries at `min_size` and `max_size`. Set
`BLOBTRACK_DISABLE_NUMBA=1` to force the fallback, which CI exercises so the
unaccelerated path stays tested.

**Measured breakdown, 24 MB file, pure-Python backend:**

| Stage | Time | Share |
|---|---|---|
| Chunking | ~4.7 s | ~89% |
| SHA-256 | ~1.0 s | ~19% |
| zstd | ~0.1 s | ~2% |

Chunking dominates, which is why the whole performance effort went there.

### The small-file fast path

A file no larger than `MIN_CHUNK_SIZE` (512 KB) yields exactly one chunk and
**never invokes the rolling hash**. This is not an approximation: tracing the
algorithm, when `size <= min_size` the scan returns `size` immediately, so the
result is provably identical to running the full CDC. Repos of many small
files would otherwise pay the full per-byte cost for nothing.

### Why boundaries are cached

CDC is deterministic: identical bytes always produce identical boundaries. So
boundaries are cached in the `chunk_cache` table, keyed by the whole-file
SHA-256. `_resolve_file_chunks()` hashes the file first (C speed, ~690 MB/s)
and only pays for chunking on a cache miss.

This makes repeat work nearly free while leaving the first pass — which is
inherently O(size) — to the compiled backend.

---

## `core/hasher.py` — Fingerprinting and Parallel Compression

### Constants

```python
STREAM_BUFFER_SIZE = 64 * 1024 * 1024   # 64 MB read buffer
```

### `hash_bytes(data) -> str`

SHA-256 as 64 lowercase hex characters. SHA-256 is used throughout — chunk
identity, Merkle nodes, and commit identity.

### `hash_file_streaming(filepath) -> str`

Hashes a whole file in 64 MB buffers without ever loading it into memory, so
a 500 GB file costs the same RAM as a 500 KB one.

### `class ProcessedChunk`

```python
@dataclass
class ProcessedChunk:
    index: int
    offset: int
    length: int
    hash: str                    # SHA-256 of the uncompressed chunk
    compressed_data: bytes
```

`has_payload` reports whether `compressed_data` holds real bytes.

### `process_chunks(chunk_stream, batch_size=16, max_workers=8, needs_payload=None)`

Hashes and compresses a chunk stream in a thread pool, yielding
`ProcessedChunk` objects **in input order**.

Batching bounds memory: at most `batch_size` chunks are in flight, so a 20 GB
file never materializes. `max_workers=8` is chosen because these are CPU-bound
in practice but zstd releases the GIL during compression.

#### `needs_payload` — why commit got faster

`needs_payload` is an optional callable taking a chunk hash and returning
whether the compressed bytes are wanted.

```python
process_chunks(stream, needs_payload=None)                    # compress everything
process_chunks(stream, needs_payload=lambda h: not store.has_chunk(h))  # compress only new
```

`blob add` passes `None` because every chunk may need writing. `blob commit`
passes a predicate that skips chunks already stored, turning a re-commit of an
unchanged repository from *hash + compress everything* into *hash only*.

On an interpreter with the native fastcdc accelerator the saving is
proportionally larger, because compression becomes a bigger share of the work.

> Measured on CPython 3.14 with pure-Python fastcdc, this optimization saves
> little (~2%) — chunking dominates there. It is correct and it will pay off on
> interpreters with the compiled accelerator, but it is not the fix for the
> current slowness. That is `fastcdc`, not this code.

---

## `core/packer.py` — Compression

```python
DEFAULT_COMPRESSION_LEVEL = 3
```

### `compress(data, level=3) -> bytes`

zstd at level 3. Measured ~1.8 GB/s on compressible data and ~860 MB/s
SHA-256 throughput, so compression is never the bottleneck. Level 3 is a
deliberate choice: level 1–2 saves little space, level 19+ costs far more time
than it saves for content-addressed storage.

### `decompress(data) -> bytes`

The inverse. Used by `checkout` when reconstructing files.

---

## `core/merkle_tree.py` — Merkle Trees

A Merkle root is a single hash that identifies an entire ordered collection.
Two collections with the same root are identical; two collections with
different roots differ somewhere, and descending the tree localizes where.

### `class MerkleNode`

```python
class MerkleNode:
    hash: str                # leaf: the chunk hash. internal: SHA-256(left + right)
    left:  MerkleNode | None
    right: MerkleNode | None
    is_leaf: bool
```

Uses `__slots__` — meaningful for deep trees over files with thousands of
chunks. Two nodes compare equal when their `hash` and `is_leaf` match.

### `_combine_hashes(left, right) -> str`

`sha256((left_hash + right_hash).encode())`. Domain separation from leaf
hashes matters: a leaf hash is a chunk's own digest, so the internal
construction is deliberately different.

### `build_tree(chunk_hashes) -> MerkleNode | None`

Builds bottom-up from an ordered list of chunk hashes. Returns `None` for an
empty list; a single chunk is its own root.

**Odd-level handling — the subtle part.** When a level has an odd number of
nodes, the leftover node is **promoted** unchanged rather than paired with a
copy of itself. Duplicating would make that chunk's hash appear twice in
`collect_leaf_hashes()`, silently corrupting any reconstruction. Promotion
guarantees every chunk is represented exactly once regardless of tree shape.

### `serialize_tree(root) -> str` / `deserialize_tree(data) -> MerkleNode`

JSON round-trip for storing the tree in the commits table.

**Cost note:** each node stores its full 64-character hash twice over (in the
node and in its parent's input), so the serialized form is roughly 230 bytes
per chunk — about 2.3 MB for a 10,000-chunk file. This is why `log` reads
history with `include_tree=False`: displaying history has no need to parse
megabytes of Merkle JSON.

### `collect_leaf_hashes(root) -> list[str]`

Every chunk hash under a node, left to right, in order. This is the ordered
recipe needed to reconstruct the original file.

---

## `core/differ.py` — Delta Computation

### `compute_delta(old_tree, new_tree) -> dict`

Positional top-down walk. When hashes match at a position, the entire subtree
is pruned into `unchanged` — a matching hash proves everything beneath it is
identical.

```
{"added": [...], "removed": [...], "unchanged": [...]}
```

**Known limitation, documented rather than hidden.** A positional walk assumes
both trees have similar *shape*. With content-defined chunking, inserting a
chunk shifts everything after it, so this under-counts shared content:

| Scenario | `compute_delta` reports |
|---|---|
| One chunk inserted at the front of 2,001 chunks | `+2001 -2001` |

It is retained because it is correct for same-shape comparisons and is faster
for large identical regions. **It is not used by `commit`, `push` or `pull`.**

### `compute_delta_by_set(old_tree, new_tree) -> dict`

Content-based comparison: collect all leaf hashes on each side and use set
arithmetic. Position-independent, therefore exact under insertion.

| Scenario | `compute_delta_by_set` reports |
|---|---|
| One chunk inserted at the front of 2,001 chunks | `+1 -0` |

Output is sorted for deterministic results. **This is the implementation the
CLI and remote sync use**, because reporting a number known to be wrong is
worse than reporting none.

---

## `core/integrity.py` — Verification

### The subtlety

A chunk is **named by the SHA-256 of its uncompressed bytes** but **stored
zstd-compressed**. So the filename cannot be compared against the bytes on
disk directly — verification must decompress first, then hash.

Skipping that step means bit rot or a truncated write stays invisible until
someone tries to restore a file. Since the store is content-addressed, checking
is cheap; there is no reason to skip it.

### `verify_chunk_payload(compressed_data, expected_hash)`

Decompresses, hashes, compares. Raises `ChunkIntegrityError` on mismatch,
truncation, or undecompressable data. The message names the chunk.

### `verify_stored_chunk(store, chunk_hash)`

Fetches from a `LocalStore` and verifies. Raises on missing or corrupt.

### `scan_chunks(store, chunk_hashes) -> (missing, corrupt)`

Verifies many chunks, **collecting** failures instead of stopping at the first.
Returns two lists so `fsck` can report everything wrong at once. Used by
`blob fsck`.
