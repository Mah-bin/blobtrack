# blobtrack — Content-Aware Binary Version Control System

A CLI tool (`like git`) for **incremental versioning of massive binary files** — videos, AI datasets, 3D models — using Content-Defined Chunking, SHA-256, Merkle Trees and Delta Synchronization.

**Problem:** `git` stores a full copy on every binary change. A 20 GB video changed 20 times = 400 GB wasted.
**Solution:** `blobtrack` slices files into variable chunks (~2 MB avg), fingerprints each with SHA-256, and stores only changed chunks. A 20 GB change becomes a ~40 MB delta.

---

## Installation

Requires Python 3.10+.

```bash
# 1. Clone
git clone https://github.com/Mah-bin/blobtrack.git
cd blobtrack

# 2. Create venv (recommended)
py -m venv venv
# Windows: venv\Scripts\activate
# Linux/Mac: source venv/bin/activate

# 3. Install dependencies
py -m pip install -r requirements.txt

# 4. Install blobtrack in editable mode (registers `blobtrack` command)
py -m pip install -e .

# Verify
blobtrack --help
```

Dependencies: `fastcdc` (CDC), `zstandard` (compression), `rich` (terminal UI), `pytest` (testing).

If `blobtrack` is not found, add `Python314\Scripts` to PATH:
```powershell
$env:Path += ";C:\Users\Admin\AppData\Local\Programs\Python\Python314\Scripts"
```

---

## Usage — Current Commands (Phase 2)

### Implemented — Increments 1 & 2

```bash
blobtrack --help          # Show all 8 commands
blobtrack --version       # Show version
blobtrack init            # Create .blobtrack repository
blobtrack add <file>      # Chunk, compress, deduplicate and store file
```

```bash
# Example - Phase 1 init
mkdir my_project && cd my_project
blobtrack init
# -> Initialized empty blobtrack repository in ...\.blobtrack
# Creates: .blobtrack/objects/ , .blobtrack/commits/ , .blobtrack/index.db (WAL SQLite)

# Example - Phase 2 add (Member 2 chunker + Member 4 storage)
# Create a test file
py -c "open('video.mp4','wb').write(b'A'*5242880 + b'B'*5242880)" # 10MB
blobtrack add video.mp4
# -> Added 'video.mp4' -> 2 chunks (2 new, 0 reused, 0.0% dedup) [10485760 -> 357 bytes compressed]

blobtrack add video.mp4
# -> Added 'video.mp4' -> 2 chunks (0 new, 2 reused, 100.0% dedup) - no duplicate objects stored

# Modify 1KB in middle, re-add
py -c "f=open('video.mp4','r+b'); f.seek(2097152); f.write(b'X'*1024); f.close()"
blobtrack add video.mp4
# -> Added 'video.mp4' -> 2 chunks (1 new, 1 reused, 50.0% dedup) - only changed chunk stored
```

`blobtrack init` is idempotent — second run reports `Error: repository already initialized` and does not delete data.
`blobtrack add` deduplicates via `has_chunk()` - identical SHA-256 chunks are stored once. Streams file via `chunk_file_streaming` + `process_chunks` (16 batch, 8 workers) so 10GB files never load fully into RAM.

### Planned — Later Increments (currently stubbed)

These commands are **registered** but return `Error: ... not available yet` until their increments are implemented:

| Command | Increment | Status |
|---|---|---|
| `blobtrack commit -m "msg"` | Inc.3 - Merkle Tree + delta diffing (Member 3+1) | stub |
| `blobtrack log` | Inc.4 - history + checkout + GC (Member 1+4) | stub |
| `blobtrack checkout <hash>` | Inc.4 | stub |
| `blobtrack gc` | Inc.4 | stub |
| `blobtrack push <remote>` | Inc.5 - remote delta sync (Member 4+3) | stub |
| `blobtrack pull <remote>` | Inc.5 | stub |

Do not consider these functional until their delivering increment passes `py -m pytest`.

---

## Project Architecture

```
              USER
                |
         blobtrack command
                |
         ┌──────────────┐
         │  cli/main.py │  argparse - 8 subcommands, dispatch
         └──────┬───────┘
                |
         ┌──────────────┐
         │ cli/commands.py │  cmd_init() + cmd_add() implemented, others integration stubs
         └──────┬───────┘
                |
    ┌───────────┴───────────┐
    ▼                       ▼
 .blobtrack/            Member 2: core/
 objects/ (LocalStore)  chunker.py (fastcdc CDC 512KB/2MB/8MB) chunk_file_streaming
 commits/               hasher.py (SHA-256 + process_chunks) ProcessedChunk
 index.db (IndexDB      packer.py (Zstandard compress) + hasher streaming
 WAL SQLite)            Member 4: storage/
                        index_db.py (IndexDB WAL, files/commits/chunks/chunk_refs)
                        local_store.py (LocalStore atomic has_chunk/store_chunk)
                        remote_sync.py (Phase 5)
```

**Member 1 (CLI & Integration Lead)** owns `cli/main.py`, `cli/commands.py`, `setup.py`, `README.md` and wires Member 2/3/4 modules.

See `docs/integration_contract.md` for confirmed vs pending APIs and `docs/cli_documentation.txt` for Phase 2 `cmd_add` 8-step flow.

---

## Development Status — Incremental (5 phases)

| Phase | What | Who Leads | Deliverable |
|---|---|---|---|
| **1** | CLI skeleton + `init` + SHA-256 | Member 1+2 | `blobtrack init` works, can hash any file - **DONE** |
| **2** | CDC chunking + compression + local storage | Member 2+4 | `blobtrack add` slices & stores deduplicated chunks - **DONE** (cli/P2) |
| 3 | Merkle Tree + delta diffing + `commit` | Member 3+1 | `blobtrack commit` detects changes |
| 4 | History + `checkout` + `gc` | Member 1+4 | `log`, `checkout`, `gc` work |
| 5 | Remote `push`/`pull` delta sync | Member 4+3 | only new chunks transferred |

Every increment produces a working demo. Current branch: `cli/P2` at Phase 2, `main` at `60f6daf` (Phase 1) until PR merged.

---

## Testing

```bash
# All tests (currently 57: 11 chunker + 21 cli + 13 hasher/index_db/local_store/remote)
py -m pytest tests/ -v

# Compile check
py -m compileall blobtrack

# Manual Phase 1-2 acceptance (in isolated C:\tmp)
mkdir C:\tmp\verify; cd C:\tmp\verify
blobtrack init
# -> .blobtrack/objects, .blobtrack/commits, .blobtrack/index.db (WAL)
blobtrack add test.bin # 240KB -> 1 chunks (1 new)
blobtrack add test.bin # second -> 0 new 1 reused 100% dedup
# Modify 1KB, re-add -> only changed chunk new (10MB -> 1 new 1 reused 50%)

# Verify hashing still works (Member 2)
py -m pytest tests/test_hasher.py tests/test_chunker.py -v
# Verify storage (Member 4)
py -m pytest tests/test_index_db.py tests/test_local_store.py -v
```

---

## Tech Stack

| Component | Technology | Why |
|---|---|---|
| Language | Python 3.10+ | Easy dev, C libs handle math |
| CLI | argparse + rich | Built-in, beautiful output |
| Chunking | fastcdc | CDC rolling-hash |
| Hashing | hashlib SHA-256 | Cryptographic fingerprint |
| Compression | zstandard | 4-5 GB/s decompress |
| DB | SQLite WAL | Zero-setup, ACID |

---

## SDGs Addressed

- **SDG 9** Industry, Innovation & Infrastructure — foundational MLOps/data engineering infra
- **SDG 12** Responsible Consumption — eliminates terabytes of redundant storage/bandwidth
