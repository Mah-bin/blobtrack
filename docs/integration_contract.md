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

## Member 3 — Merkle Tree & Delta — CONFIRMED (Increment 3) — Phase 3 Active

Source: `blobtrack/core/merkle_tree.py:59` `differ.py:17` — verified 43d89d0, 60f6daf.

```
core/merkle_tree.py — CONFIRMED
  class MerkleNode(hash, left, right, is_leaf)  # hash: str, left/right: Optional[MerkleNode]
  _combine_hashes(left_hash: str, right_hash: str) -> str  # sha256(left+right)
  build_tree(chunk_hashes: List[str]) -> Optional[MerkleNode]  # ordered list, odd level promotes not duplicates, single chunk is root
  serialize_tree(root: Optional[MerkleNode]) -> str  # JSON string
  deserialize_tree(data: str) -> Optional[MerkleNode]
  collect_leaf_hashes(root: Optional[MerkleNode]) -> List[str]  # left-to-right

core/differ.py — CONFIRMED
  compute_delta(old_tree: Optional[MerkleNode], new_tree: Optional[MerkleNode]) -> Dict[str,List[str]]  # positional top-down prunes matching hash, returns {added,removed,unchanged}
  compute_delta_by_set(old_tree, new_tree) -> Dict  # set-arithmetic exact for insert shifts, use for push/pull
```

**Contract:** Member 3 — please do not change these signatures without notifying Member 1. `cmd_commit()` depends on `build_tree` + `compute_delta`.

### Phase 3 — Commit Contracts — 9 Contracts — PENDING TEAM APPROVAL for 2 items

**Contract 1 — Current state:** `cmd_commit()` obtains current state via `IndexDB.list_files()` sorted by `path` posix, then re-chunks each tracked file on disk via `chunk_file_streaming + process_chunks` to get ordered chunk hashes. Files missing on disk are skipped with warning.

**Contract 2 — Repository Merkle representation — CURRENT IMPLEMENTATION (needs explicit team approval):**
```
Repository state:
- Files sorted lexicographically by repo-relative posix path (e.g., a.txt before b.txt)
- Chunks within each file sorted by chunk_order/index (0,1,2...)
- Chunk hashes concatenated in that deterministic order -> combined_hashes: List[str]
- combined_hashes -> build_tree() -> repository-level Merkle root
- Merkle root represents entire tracked repository snapshot
```
Alternative considered: per-file roots then hash of file roots — CURRENT chooses single repo tree over concatenated hashes. Team to confirm.

**Contract 3 — Chunk ordering:** File path sorted + chunk.index order, not DB insertion order or set.

**Contract 4 — Merkle API:** `build_tree(combined_hashes)` -> `root.hash` is merkle_root, `serialize_tree(root)` is tree_data JSON string.

**Contract 5 — Delta API:** `compute_delta(parent_tree, new_tree)` positional, for `push/pull` use `compute_delta_by_set` per Member 3 — Member 3 to confirm.

**Contract 6 — Parent:** `first commit parent=None`, `later parent = get_latest_commit().commit_hash`.

**Contract 7 — Commit hash — CURRENT IMPLEMENTATION (needs explicit team approval):**
```
commit_hash = hash_bytes(f"{merkle_root}:{message}:{timestamp}:{parent or ''}".encode())
```
SHA-256 of `merkle_root : message : timestamp : parent`. Includes timestamp for uniqueness, parent for chain. Team to confirm if `author` or `tree_data` should be included instead. Currently implemented and verified working — freeze only after team approval.

**Contract 8 — save_commit():** `IndexDB.save_commit(commit_hash,message,parent_hash,author,timestamp,merkle_root_hash,tree_data,file_chunk_mappings)` where `file_chunk_mappings` is List[Dict{file_path,chunk_hash,chunk_offset,chunk_length,chunk_order,size_uncompressed,size_compressed}] with FK CASCADE. Member 4 to confirm.

**Contract 9 — Multi-file semantics:** One commit contains coherent snapshot of ALL tracked files at commit time (files sorted, combined hashes). Changing `a.txt` only, `b.txt` unchanged is still snapshot of both. Tested with `a.txt`+`b.txt` two-file repo — pending final multi-file verification.

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

## Phase 5 — Remote Sync: Push/Pull CLI Integration — COMPLETE

Source: `blobtrack/cli/commands.py:cmd_push()`, `cmd_pull()` — Phase 5, branch `cli/P5`.

**Contract 10 — Push delegation:** `cmd_push(remote)` delegates entirely to `RemoteSync.push(remote_path, local_store, local_db)`. The CLI does NOT compute deltas; `RemoteSync` handles delta detection internally via `has_chunk()`. Returns stats dict `{transferred_chunks, transferred_bytes, skipped_chunks, commits_synced}`.

**Contract 11 — Pull delegation:** `cmd_pull(remote)` delegates to `RemoteSync.pull(remote_path, local_store, local_db)`. The CLI validates the remote exists before calling. Pull does NOT modify the working tree — user must explicitly `checkout` after pull.

**Contract 12 — Remote path semantics:** No persistent alias system. "origin" is treated as a literal filesystem path. Relative paths resolved against cwd. `RemoteSync._resolve_remote_paths` handles both `repo/` and `repo/.blobtrack/` paths.

**Contract 13 — Remote initialization:** `RemoteSync.push()` calls `init_remote()` internally on first push. The CLI does NOT need to pre-create remote structure. `cmd_pull()` validates remote `.blobtrack/objects/` exists before calling.

**Contract 14 — Delta ownership:** Delta calculation for push/pull lives entirely in `RemoteSync` (Member 4). CLI's job is validate → delegate → display. No `compute_delta_by_set()` calls from CLI.

**Test coverage:** 26 new tests in `test_remote_cli.py`:
- Push: first, repeat, incremental, multi-file, parent chain, no commits, no repo, invalid path
- Pull: existing repo, fresh clone, repeat, no repo, missing remote, invalid structure
- Round-trip: A→push→remote→pull→B→checkout (SHA-256 exact match)
- Bidirectional: A→push→B→modify→push→A→pull→checkout (both versions)
- Dedup: first=all new, incremental=skips, zero-transfer, pull zero
- GC interaction: GC → push → pull → checkout
- Subprocess: push/pull via `blobtrack` CLI invocation

---

## Status

- [x] Phase 0 — Member 2 interfaces inspected, 20 tests pass
- [x] Phase 1 — CLI foundation + init complete (main.py + commands.py + empty index.db)
- [x] Phase 2 — `blobtrack add` integration complete (Member 2 + Member 4)
- [x] Phase 3 — `blobtrack commit` integration complete (Member 3 + Member 4, Merkle tree + delta)
- [x] Phase 4 — `blobtrack log`, `checkout`, `gc` complete (Member 4 IndexDB + LocalStore)
- [x] Phase 5 — `blobtrack push`, `pull` complete (Member 4 RemoteSync, 99 tests passing)
- [x] Integration contract documented (14 contracts)

