# BlobTrack — Integration Contract
**Member 1 — CLI & Integration Lead**

This document records the function signatures Member 1 will call. Member 2 interfaces are **confirmed** (already passing tests). Member 4 interfaces are **pending confirmation** — Member 4 should confirm final signatures before Phase 2 integration of `blobtrack add`.

---

## Member 2 — Chunking & Hashing Engine — CONFIRMED

Source: `blobtrack/core/chunker.py:29`, `hasher.py:8`, `packer.py:6` — verified Phase 0, 20 tests pass.

```
core/chunker.py
  MIN_CHUNK_SIZE = 512 * 1024
  AVG_CHUNK_SIZE = 2 * 1024 * 1024
  MAX_CHUNK_SIZE = 8 * 1024 * 1024
  class Chunk(index:int, offset:int, length:int, hash:str, data:bytes)
  chunk_file(filepath: str) -> List[Chunk]          # fastcdc, hashes via hash_bytes, FileNotFoundError/ValueError
  chunk_file_streaming(filepath: str) -> Generator[Chunk]  # same but yields
  get_file_info(filepath: str) -> dict

core/hasher.py
  STREAM_BUFFER_SIZE = 64 * 1024 * 1024
  hash_bytes(data: bytes) -> str                    # SHA-256 hex 64 chars
  hash_file_streaming(filepath: str) -> str
  hash_chunks_parallel(chunks_data: List[Tuple[int,bytes]], max_workers=8) -> List[Tuple[int,str]]

core/packer.py
  DEFAULT_COMPRESSION_LEVEL = 3
  compress(data: bytes, level: int = 3) -> bytes   # zstandard
  decompress(data: bytes) -> bytes
```

**Contract:** Member 2 — please do not change these signatures without notifying Member 1. `cmd_add()` will depend on `chunk_file` + `compress`.

---

## Member 4 — Storage, Database & Remote Sync — PENDING CONFIRMATION

Expected responsibilities per project spec (Increment 2: CDC + compression + local storage). Member 4 to confirm final signatures/return types.

```
storage/index_db.py — PENDING
  Expected: init_db(db_path: Path) -> None
            Creates SQLite in WAL mode, tables: files, commits, chunks, chunk_refs
            Must use IF NOT EXISTS so Phase 1 empty index.db can be upgraded
            Member 1 will call init_db(.blobtrack/index.db) from cmd_init() after Member 4 delivers

storage/local_store.py — PENDING
  Expected: store_chunk(chunk_hash: str, compressed_data: bytes) -> None
            Writes to .blobtrack/objects/<hash>
            retrieve_chunk(chunk_hash: str) -> bytes
            has_chunk(chunk_hash: str) -> bool  # deduplication check

storage/ — future increments
  save_commit / get_commit / list_commits  (Increment 3/4)
  push / pull / garbage_collect            (Increment 4/5)
```

**Member 1 Phase 1 placeholder:** `cmd_init()` currently creates empty `index.db` via `Path.touch()` (no tables). After Member 4 confirms `init_db()`, `commands.py:cmd_init()` will change from `touch()` to `init_db(db_path)` (3-line change, no schema duplication in CLI).

**Member 4 — please confirm:** exact function names, argument order/types, return types, and error behaviour (e.g., FileNotFoundError vs bool for has_chunk) before Phase 2 integration.

---

## Member 3 — Merkle Tree & Delta — FUTURE (Increment 3)

```
core/merkle_tree.py — NOT YET (Increment 3)
  Expected: class MerkleNode(left, right, hash), build_tree(chunk_hashes), serialize/deserialize

core/differ.py — NOT YET (Increment 3)
  Expected: compute_delta(old_tree, new_tree) -> DeltaManifest(added, removed, unchanged)
```

Member 1 will integrate via `cmd_commit()` only after Increment 3.

---

## Integration Flow — Phase 2 `blobtrack add` (when Member 4 ready)

```
blobtrack add <file>
  -> main.py: parse filepath
  -> commands.py:cmd_add(filepath)
       -> verify repo .blobtrack exists, file exists, inside repo
       -> Member 2: chunk_file(filepath) -> List[Chunk]
       -> for each Chunk: compress(data) -> has_chunk(hash) ? skip : store_chunk(hash, compressed) -> register in index_db
```

CLI will stay `main.py` unchanged; only `commands.py:cmd_add()` implementation will be replaced.

---

## Status

- [x] Phase 0 — Member 2 interfaces inspected, 20 tests pass
- [x] Phase 1 — CLI foundation + init complete (main.py + commands.py + empty index.db)
- [x] Integration contract documented
- [ ] Awaiting Member 4 confirmation of storage APIs before Phase 2 `add` integration
