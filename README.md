# blobtrack — Content-Aware Binary Version Control

> A `git`-like CLI (`blob`) for **incremental versioning of massive binary files**
> (videos, AI datasets, 3D models) using Content-Defined Chunking, SHA-256,
> Merkle trees, and delta synchronization.

[![Python](https://img.shields.io/badge/python-3.10+-blue)]()
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Status:** v0.2.0 · **210 tests passing** · CI runs lint + tests on Python 3.10–3.14

---

## 1. What problem this solves

Git stores a full copy of every changed file. Commit a 20 GB video twenty times
and you have used 400 GB to store 20 GB of unique content.

blobtrack slices files into ~2 MB variable-sized chunks, fingerprints each
chunk with SHA-256, and stores each distinct chunk exactly once. A 1 KB edit
inside a 20 GB file creates one new chunk, not a new 20 GB blob.

```
   CHUNK  ────────►  FINGERPRINT  ────────►  COMPARE  ────────►  STORE
 slice with         SHA-256 per chunk     Merkle delta        only chunks
 content-defined    so identical data     old vs new         not already
 chunking           has one identity      so we move         present
```

### Why content-defined chunking

Fixed-size chunking breaks on insertion. Add one byte to the front of a file
and every boundary shifts, so every chunk changes and deduplication collapses
to nothing.

blobtrack cuts where the *data* says to cut, using a rolling hash. Inserting
bytes only disturbs the chunks around the insertion point; everything else
keeps its identity. Limits are 512 KB min / 2 MB average / 8 MB max.

---

## 2. Quick start

```bash
git clone https://github.com/Mah-bin/blobtrack.git
cd blobtrack

python -m venv venv
# Windows:  venv\Scripts\activate
# Linux/Mac: source venv/bin/activate

pip install -e ".[dev]"     # registers the `blob` command
blob --help
```

### A complete session

```bash
mkdir demo && cd demo
blob init

# Create and track a 10 MB file (5 MB of 'A' then 5 MB of 'B')
python -c "open('video.mp4','wb').write(b'A'*5242880 + b'B'*5242880)"
blob add video.mp4
# Added 'video.mp4' -> 2 chunks (2 new, 0 reused, 0.0% dedup)
#   [10485760 -> 357 bytes compressed] in 0.1s

blob commit -m "first version"
# Committed ca6543a654c6 - 1 file(s), 2 chunks (2 new), root a57493c037eb...
# branch main | parent - -> ca6543a654c6 in 0.2s

# Modify 1 KB near the middle
python -c "f=open('video.mp4','r+b'); f.seek(2097152); f.write(b'X'*1024); f.close()"
blob add video.mp4
# Added 'video.mp4' -> 2 chunks (1 new, 1 reused, 50.0% dedup)

blob commit -m "second version"
# | delta: +1 -0 =1        <- only the changed chunk is new

blob log
blob fsck                  # verify every referenced chunk
blob checkout ca6543a654c6 # restore; every chunk is SHA-256 verified
```

---

## 3. Commands

| Command | What it does |
|---|---|
| `blob init` | Create a repository in the current directory |
| `blob add <file>` | Chunk, hash, compress and store a file; reuse existing chunks |
| `blob commit -m <msg>` | Snapshot every tracked file as an immutable commit |
| `blob log` | Show history, newest first |
| `blob checkout <ref>` | Restore a commit or branch; verifies every chunk |
| `blob rm <path>` | Stop tracking a path (does **not** rewrite history) |
| `blob fsck` | Verify integrity: missing and corrupt chunks. Exit 1 if broken |
| `blob gc [--dry-run]` | Delete chunks no commit references |
| `blob migrate [--dry-run]` | Move legacy flat chunks into the fan-out layout |
| `blob branch [name] [-d]` | List branches, or create/delete one |
| `blob switch <branch>` | Move the current branch pointer |
| `blob merge <branch>` | Join two branches (fast-forward, or union) |
| `blob push [remote]` | Delta-push commits and the chunks they need |
| `blob pull [remote]` | Delta-pull; does not touch the working tree |

`blobtrack` is an alias for `blob`.

### Safety properties

These are enforced in code and covered by tests, not just documented:

- **A missing tracked file fails the commit.** blobtrack will not silently
  drop a file from history. Delete the file, then `blob rm` it, then commit.
- **`checkout` cannot write outside the repository.** Paths from the database
  are treated as untrusted — absolute paths and `..` are rejected. This
  matters because a remote can supply them via `pull`.
- **Every chunk is verified before it is used.** Each chunk is named by the
  SHA-256 of its uncompressed content, and `checkout` verifies that before
  writing bytes to your disk.
- **Output never lies.** Success messages print only after the durable write
  succeeds. Errors go to stderr and exit non-zero. User text is never parsed
  as markup.
- **`fsck` is honest.** It verifies content, not just presence, and exits 1 if
  anything is wrong — safe to use in CI.

---

## 4. How it works

### Layout on disk

```
.blobtrack/
├── objects/
│   ├── .tmp/                      staging area for atomic writes
│   └── ab/abcdef0123...           two-character fan-out, then full hash
├── commits/                       reserved
└── index.db                       SQLite (WAL): files, commits, chunks,
                                   chunk_refs, refs, config, commit_parents
```

Chunks are stored **zstd-compressed** but **named by the SHA-256 of their
uncompressed bytes**, which is what makes deduplication work across files,
versions and repositories.

### Pipeline

1. **Chunk** — `chunk_file_streaming` yields one chunk at a time, holding a
   single file handle open. Memory is flat regardless of file size.
2. **Fingerprint and compress** — `process_chunks` hashes and compresses in a
   thread pool, in batches of 16 across 8 workers, preserving order.
3. **Deduplicate** — a chunk already in the store is never rewritten.
4. **Snapshot** — `build_tree` produces a Merkle root over the ordered chunk
   hashes of every tracked file. That root *is* the repository state.
5. **Delta** — `compute_delta_by_set` reports which chunks are genuinely new.

### Why the delta is set-based, not positional

A positional tree walk compares chunks by position. With content-defined
chunking, inserting one chunk early shifts everything after it, so a
positional diff reports thousands of changes where only one chunk is new.

Measured on a 2,001-chunk file with a single chunk inserted at the front:

| Method | Result |
|---|---|
| `compute_delta` (positional) | `+2001 -2001` — reports 4,002 changed chunks |
| `compute_delta_by_set` (used) | `+1 -0` — correct |

`push` and `pull` both use the set-based diff, walking from the newest commit
the two sides already share. This is why `push` transfers only what is new
rather than scanning every object ever written.

### Performance

Measured on a 24 MB file (CPython 3.14, where no `fastcdc` wheel exists):

| Stage | Time | Share |
|---|---|---|
| Content-defined chunking | ~5.6 s | ~89% |
| SHA-256 of every chunk | ~1.2 s | ~19% |
| zstd level 3 | ~0.1 s | ~2% |

Chunking dominates. `process_chunks` supports `needs_payload` so `commit` skips
compression for chunks it already has, but on this interpreter that saves
little because the pure-Python rolling hash holds the GIL and cannot be
parallelized.

> **Performance note:** `fastcdc` ships a Cython accelerator. Wheels exist for
> CPython 3.10–3.13. On 3.14 the library falls back to pure Python and prints
> its own warning; blobtrack detects this and warns too. Use 3.10–3.13 for
> real throughput.

`blob commit` re-reads and re-hashes every tracked file on each run. That is
linear in repository size, not in delta size — it is a deliberate simplicity
tradeoff, and the place to optimize next.

---

## 5. Architecture

```
                    USER
                      |
                  blob command
                      |
        +-------------+-------------+
        |                           |
  cli/main.py                 cli/commands.py
  (argparse, 14 subcommands)  (validation -> delegate -> report)
        |                           |
        +-------------+-------------+
                      |
     +----------------+-----------------+
     |                |                 |
 core/chunker   core/hasher      core/packer
 fastcdc CDC    SHA-256 +        zstd level 3
                thread pool
     |
 core/merkle_tree.py  build/serialize trees
 core/differ.py       content delta
 core/integrity.py    chunk verification
     |
 +---+------------------+------------------+
 |                    |                  |
 storage/paths.py  storage/local_store.py  storage/index_db.py
 repo-root          content-addressed    SQLite metadata,
 confinement        object store         commits, refs
                    + legacy fallback
                    |
             storage/remote_sync.py
             delta push / pull
```

Layering rule: `core/` knows nothing about storage; `storage/` knows nothing
about the CLI. The CLI validates, delegates, and reports — it does not
reimplement chunking, hashing, or storage.

### Module map

| File | Responsibility |
|---|---|
| `core/chunker.py` | Content-defined chunking; detects the fastcdc backend |
| `core/hasher.py` | SHA-256, parallel compress pipeline, commit-hash derivation |
| `core/packer.py` | zstd compress/decompress |
| `core/merkle_tree.py` | Merkle tree construction and serialization |
| `core/differ.py` | Positional and content-based deltas |
| `core/integrity.py` | Chunk payload verification |
| `storage/paths.py` | Path normalization and repository containment |
| `storage/local_store.py` | Object store, fan-out layout, legacy fallback, migration |
| `storage/index_db.py` | Schema, commits, files, chunks, refs, ancestry |
| `storage/remote_sync.py` | Delta push and pull |
| `cli/commands.py` | Command handlers |
| `cli/main.py` | Argument parsing and dispatch |

---

## 6. Testing

```bash
pip install -e ".[dev]"
pytest tests/ -v              # 210 tests
ruff check .                  # lint
python -m compileall blobtrack
```

| Suite | Covers |
|---|---|
| `test_chunker.py` | CDC boundaries, contiguity, determinism, round-trip |
| `test_hasher.py` | SHA-256 correctness, order preservation, compression round-trip |
| `test_merkle_delta.py` | Tree shape, serialization, and delta correctness |
| `test_paths.py` | Traversal and absolute-path rejection |
| `test_integrity.py` | Corruption, truncation, verification |
| `test_store_layout.py` | Fan-out, legacy fallback, migration |
| `test_index_db.py` | Schema, commits, refs, orphans, ancestry |
| `test_local_store.py` | Object store basics and GC |
| `test_remote_sync.py` | Delta push/pull unit behaviour |
| `test_repo_commands.py` | rm, fsck, gc, migrate, branch, switch, merge |
| `test_cli.py` | CLI surface and add/commit/log/checkout integration |
| `test_remote_cli.py` | Push/pull integration and round-trips |

The CI workflow additionally verifies that the test count in this README
matches reality, that every documented command exists, and runs an end-to-end
smoke test — so the documentation cannot drift away from the code.

---

## 7. Known limitations

Stated plainly, because a system that hides its limits is harder to trust:

- **`push`/`pull` are filesystem paths, not network transports.** There is no
  SSH, HTTP, authentication, or encryption. A "remote" is a directory you
  already trust.
- **No file-level three-way merge.** `merge` unions files by content hash and
  reports conflicts rather than resolving them, which is the right default for
  binaries but is not a text merge.
- **No `status`, `diff`, or `show` commands.** History is inspected via `log`.
- **History is append-only.** There is no way to drop a commit or reclaim the
  space its chunks occupied. `rm` untracks a path going forward; past commits
  keep their chunks.
- **`commit` cost scales with repository size**, not change size (see
  Performance above).
- **No locking.** Two concurrent `blob add` on the same file can interleave
  metadata writes. SQLite transactions keep the database consistent, but the
  operations are not serialized at the CLI level.
- **Single branch checkout is full-tree.** `checkout` writes every file in the
  commit; there is no sparse or incremental checkout.

---

## 8. Team

| Area | Module |
|---|---|
| Chunking, hashing, compression | `core/chunker.py`, `core/hasher.py`, `core/packer.py` |
| Merkle trees, delta diffing | `core/merkle_tree.py`, `core/differ.py` |
| Storage, database, remote sync | `storage/` |
| CLI and integration | `cli/`, `pyproject.toml`, docs |

## 9. License

MIT — see [LICENSE](LICENSE).
