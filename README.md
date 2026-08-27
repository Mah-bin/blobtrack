# blobtrack — Content-Aware Binary Version Control System

> A `git`-like CLI for **incremental versioning of massive binary files** (videos, AI datasets, 3D models) using **Content-Defined Chunking, SHA-256, Merkle Trees, and Delta Synchronization**.

![Python](https://img.shields.io/badge/python-3.10+-blue) ![Tests](https://img.shields.io/badge/tests-57_passed-brightgreen) ![Status](https://img.shields.io/badge/phase-3_done-green)

**Problem:** `git` stores a full 20 GB binary copy on every change → 20 commits = 400 GB wasted.
**Solution:** `blobtrack` slices files into ~2 MB variable chunks, fingerprints each with SHA-256, and stores **only changed chunks**. A 20 GB edit becomes a ~40 MB delta.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Features](#2-features)
3. [Installation](#3-installation)
4. [Quick Start](#4-quick-start)
5. [Usage](#5-usage)
6. [Project Architecture](#6-project-architecture)
7. [Project Structure](#7-project-structure)
8. [Development Status](#8-development-status)
9. [Testing](#9-testing)
10. [Tech Stack](#10-tech-stack)
11. [Documentation](#11-documentation)
12. [Team Roles](#12-team-roles)
13. [SDGs Addressed](#13-sdgs-addressed)

---

## 1. Overview

`blobtrack` enables true incremental versioning of large binaries without wasting storage or bandwidth. The 4-step pipeline:

```
1. CHUNK  → 2. FINGERPRINT → 3. COMPARE → 4. SYNC
Slice file   SHA-256 hash    Merkle Tree   Store only
into pieces  each chunk      old vs new    new chunks
using CDC                                   (deduplicated)
```

* **CHUNK:** Stream file in 64 MB buffers, slice via `fastcdc` into variable chunks (min 512 KB, avg 2 MB, max 8 MB). CDC ensures inserting 1 byte only affects nearby chunks.
* **FINGERPRINT:** SHA-256 per chunk (64-char hex). Identical data = identical hash.
* **COMPARE:** Merkle Tree built bottom-up from chunk hashes; top-down diff prunes identical subtrees.
* **SYNC:** Only new/modified chunks compressed with Zstandard and stored in `.blobtrack/objects/`.

## 2. Features

*   **Incremental:** 10 MB file change of 1 KB → only 1 new chunk stored (50% dedup for 2-chunk file, 99% for 5000-chunk file)
*   **Deduplicated:** `has_chunk()` check prevents duplicate storage
*   **Versioned:** `commit` snapshots with Merkle root + parent chain, delta `+1 -1 =0` vs previous
*   **Streaming:** Never loads whole file into RAM (`chunk_file_streaming` + `process_chunks` batch 16, workers 8)
*   **Atomic:** `LocalStore` writes via `tempfile + fsync + atomic move`, `IndexDB` WAL mode with `IF NOT EXISTS`
*   **8 CLI commands:** `init, add, commit, log, checkout, push, pull, gc` (3 implemented, 5 stubbed)

## 3. Installation

**Prerequisites:** Python 3.10+, `pip`

```bash
# Clone
git clone https://github.com/Mah-bin/blobtrack.git
cd blobtrack

# Venv (recommended)
py -m venv venv
# Windows: venv\Scripts\activate
# Linux/Mac: source venv/bin/activate

# Dependencies
py -m pip install -r requirements.txt
# fastcdc, zstandard, rich, pytest

# Editable install (registers `blobtrack` command)
py -m pip install -e .

# Verify
blobtrack --help
```

> If `blobtrack` not found: `$env:Path += ";C:\Users\Admin\AppData\Local\Programs\Python\Python314\Scripts"` (Windows)

## 4. Quick Start

```bash
mkdir demo && cd demo
blobtrack init
# Initialized empty blobtrack repository in ...\.blobtrack

# Create a file, add and commit
py -c "open('video.mp4','wb').write(b'A'*5242880 + b'B'*5242880)" # 10 MB
blobtrack add video.mp4
# Added 'video.mp4' -> 2 chunks (2 new, 0 reused, 0.0% dedup) [10485760 -> 357 bytes compressed]

blobtrack commit -m "first version"
# Committed ca6543a654c6 - 1 file(s), 2 chunks, root a57493c037eb... - "first version"

# Modify 1KB, re-add and commit
py -c "f=open('video.mp4','r+b'); f.seek(2097152); f.write(b'X'*1024); f.close()"
blobtrack add video.mp4
# Added 'video.mp4' -> 2 chunks (1 new, 1 reused, 50.0% dedup)
blobtrack commit -m "second version"
# Committed 01de1f33cb00 - 1 file(s), 2 chunks, root 54e9f75c1140... | delta: +1 -1 =1 - "second version"
# parent ca6543a654c6 -> 01de1f33cb00
```

## 5. Usage

### 5.1 Implemented Commands

| Command | Description | Example | Status |
|---|---|---|---|
| `blobtrack --help` | Show all 8 commands | `blobtrack --help` | ✅ |
| `blobtrack --version` | Show version `0.1.0` | `blobtrack --version` | ✅ |
| `blobtrack init` | Create repo in current dir | `blobtrack init` | ✅ Phase 1 |
| `blobtrack add <file>` | Chunk, compress, deduplicate, store | `blobtrack add video.mp4` | ✅ Phase 2 |
| `blobtrack commit -m "msg"` | Snapshot current state with Merkle root + parent | `blobtrack commit -m "v1"` | ✅ Phase 3 |

**`blobtrack init`:** Creates `.blobtrack/objects/`, `.blobtrack/commits/`, `.blobtrack/index.db` (WAL SQLite, `0o700`). Idempotent — second run: `Error: repository already initialized` (no delete).

**`blobtrack add <file>`:**
*   Validates repo exists (walk up parents hunting `.blobtrack/`) and file exists/is_file
*   Streams via `chunk_file_streaming` → `process_chunks` → `has_chunk`/`store_chunk`/`record_chunk` → `register_file`
*   Output: `Added 'rel/path' -> N chunks (new, reused, dedup% [uncompressed -> compressed])`
*   Handles relative/absolute paths with spaces, empty files, missing files, directories — all controlled `Error:` + `exit 1`

**`blobtrack commit -m "msg"`:**
*   Validates `message` non-empty and repo + tracked files exist (`list_files()` sorted posix)
*   Re-chunks each tracked file via `chunk_file_streaming -> process_chunks`, collects `combined_hashes` ordered by file path + chunk index (Contract 3)
*   `build_tree(combined)` -> `root.hash` + `serialize_tree(root)` -> `merkle_root` + `tree_data`
*   Parent: `get_latest_commit()` -> `parent_hash = latest.commit_hash` else `None` for first commit
*   Commit hash: `hash_bytes(f"{merkle_root}:{message}:{timestamp}:{parent or ''}".encode())` deterministic
*   Delta: `compute_delta(parent_tree,new_tree)` -> `| delta: +1 -1 =1` for logging
*   Persists atomically via `IndexDB.save_commit(...,tree_data,file_chunk_mappings)` with `offset/length/order`
*   Output: `Committed <12> - N file(s), M chunks, root <12>... | delta ... - "msg"` + dim parent link
*   Handles `no tracked files`, `file missing on disk` (skip with warning), `empty` -> `exit 1`

**Deduplication & Versioning Examples:**
```bash
blobtrack add test.bin        # 240 KB (<512KB) -> 1 chunks (1 new)
blobtrack add test.bin        # same file -> 0 new 1 reused 100% (objects stay 1)
blobtrack commit -m "v1"      # first commit parent None root 01a19c
blobtrack commit -m "v2"      # same content -> same root 01a19c parent v1, delta +0
# 10 MB 2-chunk file, patch 1KB at 2MB, add + commit -> 1 new 1 reused 50% delta +1 -1
```

### 5.2 Planned Commands (Stubbed)

Registered in `argparse` but currently return `Error: ... not available yet` (`exit 1`):

| Command | Increment | Status |
|---|---|---|
| `blobtrack log` | Inc.4 history + GC (Member 1+4) | ⏳ stub |
| `blobtrack checkout <hash>` | Inc.4 | ⏳ stub |
| `blobtrack gc` | Inc.4 | ⏳ stub |
| `blobtrack push <remote>` | Inc.5 remote sync (Member 4+3) | ⏳ stub |
| `blobtrack pull <remote>` | Inc.5 | ⏳ stub |

> `push`/`pull` default to `origin` if no remote given (`nargs="?" default="origin"`).

## 6. Project Architecture

```
                         USER
                           |
                    blobtrack command
                           |
                    ┌──────────────┐
                    │  cli/main.py │  build_parser() -> 8 subcommands, main() dispatch
                    └──────┬───────┘
                           |
                    ┌──────────────┐
                    │cli/commands.py│  cmd_init() ✅ + cmd_add() ✅ + cmd_commit() ✅ + 5 stubs
                    └──────┬───────┘
                           |
              ┌────────────┴─────────────┐
              ▼                          ▼
       .blobtrack/                   Member 2: core/
       ├── objects/ (LocalStore)     ├── chunker.py  chunk_file_streaming -> ChunkData
       ├── commits/                  ├── hasher.py   process_chunks -> ProcessedChunk(hash,compressed)
       └── index.db (IndexDB WAL)    └── packer.py   compress/decompress (zstd)
                                    Member 3: core/
                                    ├── merkle_tree.py build_tree/serialize_tree root.hash
                                    └── differ.py compute_delta (positional prune)
                                    Member 4: storage/
                                    ├── index_db.py IndexDB (files/commits/chunks/chunk_refs, save_commit)
                                    ├── local_store.py LocalStore (has_chunk/store_chunk atomic)
                                    └── remote_sync.py RemoteSync (Phase 5)
```

**Member 1 (CLI & Integration Lead)** owns `cli/main.py`, `cli/commands.py`, `setup.py`, `README.md` and wires others — the glue.

## 7. Project Structure

```
blobtrack/
├── blobtrack/
│   ├── __init__.py
│   ├── cli/
│   │   ├── __init__.py
│   │   ├── main.py          # Member 1 - argparse front door
│   │   └── commands.py      # Member 1 - cmd_init + cmd_add + cmd_commit integration
│   ├── core/
│   │   ├── __init__.py
│   │   ├── chunker.py       # Member 2 - CDC 512KB/2MB/8MB
│   │   ├── hasher.py        # Member 2 - SHA-256 + parallel + ProcessedChunk
│   │   ├── packer.py        # Member 2 - Zstd
│   │   ├── merkle_tree.py   # Member 3 - Merkle Tree
│   │   └── differ.py        # Member 3 - delta diff
│   └── storage/
│       ├── __init__.py
│       ├── index_db.py      # Member 4 - WAL SQLite
│       ├── local_store.py   # Member 4 - atomic object store
│       └── remote_sync.py   # Member 4 - delta push/pull
├── tests/
│   ├── test_chunker.py
│   ├── test_hasher.py
│   ├── test_cli.py          # Member 1 - 21 CLI + Phase 3 commit integration tests
│   ├── test_index_db.py     # Member 4
│   ├── test_local_store.py
│   └── test_remote_sync.py
├── docs/
│   ├── cli_documentation.txt
│   ├── core_engine_documentation.txt
│   ├── storage_documentation.md
│   └── integration_contract.md
├── requirements.txt
├── setup.py
└── README.md
```

## 8. Development Status

Incremental, each phase produces a working demo. Current branch: `cli/P3` at Phase 3, `main` at `60f6daf` until PR merged.

| Phase | What | Who Leads | Deliverable | Status |
|---|---|---|---|---|
| **1** | CLI skeleton + `init` + SHA-256 | Member 1+2 | `blobtrack init` works, can hash any file | **DONE** |
| **2** | CDC chunking + compression + local storage | Member 2+4 | `blobtrack add` slices & stores deduplicated | **DONE** `cli/P2` |
| **3** | Merkle Tree + delta diffing + `commit` | Member 3+1 | `blobtrack commit` builds tree, detects changes, persists snapshot | **DONE** `cli/P3` |
| 4 | History + `checkout` + `gc` | Member 1+4 | `log`/`checkout`/`gc` work | ⏳ Next |
| 5 | Remote `push`/`pull` delta sync | Member 4+3 | only new chunks transferred | ⏳ |

## 9. Testing

```bash
# All tests (57: 11 chunker + 21 cli + 13 hasher/index_db/local_store/remote + Phase 3 commit)
py -m pytest tests/ -v

# Compile check
py -m compileall blobtrack

# Manual Phase 1-3 acceptance (isolated C:\tmp)
mkdir C:\tmp\verify; cd C:\tmp\verify
blobtrack init
blobtrack add test.bin        # 240KB -> 1 chunks (1 new)
blobtrack commit -m "v1"      # first commit parent None root 01a19c
blobtrack add test.bin
blobtrack commit -m "v2"      # second same file same root parent v1
# Modify 1KB, add + commit -> delta +1 -1, new root 348a7d
# 10MB 2 chunks -> 1 new 1 reused 50% after 1KB patch

# Specific suites
py -m pytest tests/test_hasher.py tests/test_chunker.py -v  # Member 2
py -m pytest tests/test_index_db.py tests/test_local_store.py -v  # Member 4
py -m pytest tests/test_cli.py -k commit -v  # Member 1 Phase 3
```

## 10. Tech Stack

| Component | Technology | Reason |
|---|---|---|
| Language | Python 3.10+ | Easy dev, C libs handle math |
| CLI | `argparse` + `rich` | Built-in + beautiful output |
| Chunking | `fastcdc` | CDC rolling-hash, variable chunks |
| Hashing | `hashlib` SHA-256 | Cryptographic fingerprint |
| Compression | `zstandard` | 4-5 GB/s decompress, high ratio |
| Merkle | `hashlib` SHA-256 | Tree of chunk hashes, serialize JSON |
| Diff | `differ.py` | Positional prune + by_set for push/pull |
| DB | SQLite (WAL mode) | Zero-setup, ACID, concurrent |
| Tests | `pytest` | Standard |

## 11. Documentation

*   `docs/cli_documentation.txt` — `main.py`/`commands.py` `cmd_add` 8-step + `cmd_commit` 9-contract flow
*   `docs/core_engine_documentation.txt` — CDC, hashing, Merkle, differ
*   `docs/storage_documentation.md` — `IndexDB`/`LocalStore` APIs
*   `docs/integration_contract.md` — Member 2/3 confirmed vs Member 4 pending APIs

## 12. Team Roles

| Member | Role | Files Owned |
|---|---|---|
| **1** | CLI & Integration Lead | `cli/main.py`, `cli/commands.py`, `setup.py`, `README.md` |
| 2 | Chunking & Hashing Engine | `core/chunker.py`, `hasher.py`, `packer.py` |
| 3 | Merkle Tree & Delta Diffing | `core/merkle_tree.py`, `differ.py` |
| 4 | Storage, Database & Remote Sync | `storage/local_store.py`, `index_db.py`, `remote_sync.py` |

## 13. SDGs Addressed

*   **SDG 9** Industry, Innovation & Infrastructure — foundational MLOps/data engineering infrastructure
*   **SDG 12** Responsible Consumption & Production — eliminates terabytes of redundant storage and bandwidth, reducing cloud energy
