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

## Usage — Current Commands (Phase 1)

### Implemented — Increment 1

```bash
blobtrack --help          # Show all 8 commands
blobtrack --version       # Show version
blobtrack init            # Create .blobtrack repository in current directory
```

```bash
# Example
mkdir my_project && cd my_project
blobtrack init
# -> Initialized empty blobtrack repository in ...\.blobtrack
# Creates: .blobtrack/objects/ , .blobtrack/commits/ , .blobtrack/index.db
```

`blobtrack init` is idempotent — second run reports `Error: repository already initialized` and does not delete data.

### Planned — Later Increments (currently stubbed)

These commands are **registered** in the CLI but return `Error: ... not available yet` until their increments are implemented:

| Command | Increment | Status |
|---|---|---|
| `blobtrack add <file>` | Inc.2 - CDC + compression + local storage (Member 2+4) | stub |
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
         │ cli/commands.py │  cmd_init() fully implemented, others integration stubs
         └──────┬───────┘
                |
    ┌───────────┴───────────┐
    ▼                       ▼
 .blobtrack/            Member 2: core/
 objects/               chunker.py (fastcdc CDC 512KB/2MB/8MB)
 commits/               hasher.py (SHA-256 streaming, parallel)
 index.db (empty        packer.py (Zstandard)
 container Phase 1)
                Storage (Member 4 - pending)
                 local_store.py, index_db.py (WAL SQLite)
```

**Member 1 (CLI & Integration Lead)** owns `cli/main.py`, `cli/commands.py`, `setup.py`, `README.md` and wires Member 2/3/4 modules.

See `docs/integration_contract.md` for confirmed vs pending APIs.

---

## Development Status — Incremental (5 phases)

| Phase | What | Who Leads | Deliverable |
|---|---|---|---|
| **1** | CLI skeleton + `init` + SHA-256 | Member 1+2 | `blobtrack init` works, can hash any file - **DONE** |
| 2 | CDC chunking + compression + local storage | Member 2+4 | `blobtrack add` slices & stores deduplicated chunks |
| 3 | Merkle Tree + delta diffing + `commit` | Member 3+1 | `blobtrack commit` detects changes |
| 4 | History + `checkout` + `gc` | Member 1+4 | `log`, `checkout`, `gc` work |
| 5 | Remote `push`/`pull` delta sync | Member 4+3 | only new chunks transferred |

Every increment produces a working demo. Current branch: `main` at Phase 1.

---

## Testing

```bash
# All tests (currently 44: 11 chunker + 21 cli + 12 hasher)
py -m pytest tests/ -v

# Compile check
py -m compileall blobtrack

# Manual Phase 1 acceptance (in isolated C:\tmp)
mkdir C:\tmp\verify; cd C:\tmp\verify
blobtrack init
# -> .blobtrack/objects, .blobtrack/commits, .blobtrack/index.db
blobtrack init  # second -> already initialized

# Verify hashing still works (Member 2)
py -m pytest tests/test_hasher.py tests/test_chunker.py -v
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
