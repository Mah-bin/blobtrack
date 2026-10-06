# CLI Reference

Every command, its exact behaviour, and the invariants it maintains.

The CLI's job is **validate → delegate → report**. It does not reimplement
chunking, hashing, Merkle trees, or storage.

---

## Global conventions

| Convention | Rule |
|---|---|
| Success | stdout, exit 0, printed **after** the durable write succeeds |
| Error | stderr, prefixed `Error:`, exit 1 |
| Markup | user-supplied text is never parsed as Rich markup |
| Ordering | `log` newest first; commits sync oldest first |

> **Why output is ordered this way.** A success line is a claim that something
> happened. Printing it before the write commits would let a crash make a
> failed operation look successful — and a printing failure after the write
> would report failure for work that actually completed. So: write first, print
> second, and never let rendering interpret user text.

---

## `blob init`

Creates `.blobtrack/objects/`, `.blobtrack/commits/`, and an initialized
`.blobtrack/index.db`.

```bash
blob init
# Initialized empty blobtrack repository in /path/to/.blobtrack
```

- Directories are created with mode `0o700` (ignored on Windows, not an error).
- The schema is built through `IndexDB`, so `init` and normal use cannot
  disagree about it.
- **Refuses** if `.blobtrack` exists — never destroys an existing repository.
- On any failure the whole directory is removed, so a half-created repository is
  never left behind.

---

## `blob add <file>`

Chunk, hash, compress, deduplicate and store a file.

```bash
blob add video.mp4
# Added 'video.mp4' -> 2 chunks (2 new, 0 reused, 50.0% dedup)
#   [10485760 -> 357 bytes compressed] in 0.1s
```

Steps:

1. Locate the repository by walking up for `.blobtrack`.
2. Resolve the path against the cwd.
3. **Reject files outside the repository root** — see `paths.py`.
4. Stream through `chunk_file_streaming` → `process_chunks` (batch 16, 8 workers).
5. `has_chunk` → reuse, otherwise `store_chunk`.
6. Record chunk metadata (idempotent).
7. `register_file` with the file hash, size and mtime.

Never loads the whole file into memory. Handles spaces in paths, relative and
absolute paths. Empty files, missing files, and directories produce a clear
error and exit 1.

Chunks already present are reused rather than rewritten — that is what makes
repeated adds of similar large files cheap.

---

## `blob commit -m <message>`

Snapshot every tracked file as an immutable commit.

```bash
blob commit -m "re-encode v2"
# Committed 01de1f33cb00 - 2 file(s), 14 chunks (1 new), root 54e9f75c1140...
#   | delta: +1 -0 =13 - "re-encode v2"
# branch main | parent ca6543a654c6 -> 01de1f33cb00 in 0.3s
```

### A missing tracked file is a hard error

If a tracked file is absent from the working tree, `commit` **fails**:

```
Error: tracked file(s) missing from the working tree: gone.bin
  Restore them, or run 'blob rm <path>' to stop tracking them.
```

**Nothing is written and the exit code is 1.** This is deliberate. The
alternative — skipping the file — let a commit silently drop a file from
history while reporting success, which is the worst failure mode a version
control system can have.

### The snapshot

- Tracked files sorted by repo-relative POSIX path; chunks ordered by index.
- `build_tree(combined_hashes)` → Merkle root identifying the whole repository.
- Parent is the previous commit **on the current branch**.
- Commit hash = `sha256(f"{merkle_root}:{message}:{timestamp}:{parent}")`.

### The delta shown

`delta: +A -B =C` comes from `compute_delta_by_set`, which compares chunk
*content* rather than position.

This matters: with content-defined chunking, inserting a chunk early shifts
everything after it. A positional walk on a 2,001-chunk file with one chunk
inserted reports `+2001 -2001`; the content-based diff correctly reports `+1 -0`.
The number displayed is one that can be trusted.

### Efficiency

Chunks already in the store are **not recompressed** — `process_chunks` receives
a `needs_payload` predicate so unchanged chunks cost only a hash. Chunks are
persisted as they are discovered, so a commit is never backed by objects that
were never written.

---

## `blob log`

Show history, newest first.

```
                    Commit history (8 commits)
  Hash          Message              Date                  Parent
  ca6543a654c6  first version        2026-08-27 12:34:29   -
  01de1f33cb00  re-encode v2         2026-08-27 12:35:02   ca6543a654c6

Displayed 8 commit(s) | branch main @ 01de1f33cb00
```

Read-only. Loads history with `include_tree=False` so it does not parse
multi-megabyte Merkle trees it will never display.

Commit messages are rendered as `rich.text.Text`, so a message containing
`[/red]` prints literally instead of raising.

---

## `blob checkout <ref>`

Restore a commit or branch into the working tree.

```bash
blob checkout ca6543a654c6     # abbreviated hash
blob checkout main             # branch name
```

Resolution order: branch name → full hash → unique hash prefix. An ambiguous
prefix is reported rather than guessed at.

### Safety

- Every restored path goes through `safe_join`, which rejects absolute paths,
  drive letters, and `..`. **A remote cannot make checkout write outside the
  repository.**
- Each chunk is read with `verify=True`: decompressed and confirmed to hash to
  the name it is stored under.
- Length is checked against the recorded `chunk_length`.
- Total size is verified before the file is moved into place.
- Files are written to a temp file and `replace`d, so a partial file is never
  visible.

Untracked files are left alone. If a required chunk is missing or corrupt, the
command reports it and exits 1 without writing a bad file.

---

## `blob rm <path>`

Stop tracking a path.

```bash
blob rm gone.bin
# Removed 'gone.bin' from tracking (file was already absent from disk)
```

This is the supported way to record a deletion. It edits the manifest going
forward — **existing commits still contain the file**, which is what makes
history trustworthy.

Works whether or not the file is still on disk, which matters because the
usual reason to `rm` is that the file is already gone.

Untracked chunks become reclaimable with `blob gc`.

---

## `blob fsck`

Verify repository integrity.

```bash
blob fsck
# Commits:            8
# Referenced chunks:  15
# Stored chunks:      15
# Stored size:        1.2 MB
# Unreferenced:       0 (reclaimable with 'blob gc')
#
# fsck OK - 15 referenced chunk(s) verified, no corruption found.
```

Checks:

1. Every chunk referenced by a commit **exists** in the object store.
2. Every such chunk **decompresses and hashes** to the name it is filed under.
3. How many chunks are unreferenced.
4. Whether chunks exist on disk the database has never heard of.

**Exits 1 if anything is wrong**, reporting missing and corrupt chunks
separately with recovery hints. Safe to use in CI.

Unlike `gc` — which compares counts and can report success on a damaged
repository — `fsck` verifies content.

---

## `blob gc [--dry-run]`

Delete chunks no commit references.

```bash
blob gc --dry-run
# Dry run: 3 object(s) would be deleted, 1.4 MB freed.
# Active chunks preserved: 15 | DB rows that would be removed: 3

blob gc
# Deleted 3 orphan chunk(s) from objects and 3 DB record(s); freed 1.4 MB.
# Active chunks preserved: 15.
```

"Alive" means referenced by **any** commit, not just the latest, so every
restorable version keeps its chunks. `--dry-run` reports without deleting.

> Because history is append-only, `gc` cannot reclaim space from superseded
> versions — those chunks are still referenced. It reclaims chunks left behind
> by untracked or abandoned work.

---

## `blob migrate [--dry-run]`

Move legacy flat-layout chunks into the fan-out layout.

```bash
blob migrate --dry-run
# Dry run: 1240 legacy chunk(s) would be moved into the fan-out layout.

blob migrate
# Migrated 1240 chunk(s) into the fan-out layout (340.2 MB); 1240 chunk(s) total.
```

Purely housekeeping. Reads already fall back to the flat layout, so this is
never required for correctness — it just makes large directories faster to list.
Idempotent and interruptible.

---

## `blob branch [name] [-d]`

```bash
blob branch              # list
blob branch feature      # create at current HEAD
blob branch -d feature   # delete
```

```
 * main        ca6543a654c6  first version
   feature     01de1f33cb00  re-encode v2
```

Creating a branch does not change the working tree. The current branch cannot
be deleted, and creating a duplicate fails.

---

## `blob switch <branch>`

Move the current branch pointer.

```bash
blob switch feature
# Switched from main to feature (now at 01de1f33cb00)
# Working tree unchanged. Run 'blob checkout feature' to restore files.
```

Pointer only — deliberately separate from `checkout`, which touches the disk.

---

## `blob merge <branch>`

Join another branch into the current one.

**Fast-forward** when the current branch is an ancestor:

```bash
blob merge feature
# Fast-forwarded main to 01de1f33cb00
```

**Union merge** when the branches have diverged. Files on only one side are
taken from that side. Files changed on **both** sides to different content are
reported as conflicts:

```
Error: 1 file(s) changed on both branches to different content:
    conflict  config.bin
  Resolve by checking out one side, re-adding the file, and committing,
  then merge again.
```

A successful union creates a real two-parent merge commit, and both parents
survive `push`/`pull`.

There is no three-way content merge, and there should not be: there is no
sensible line-level merge for a 20 GB video. Reporting the conflict is the
honest outcome. A failed merge leaves the branch exactly where it was.

---

## `blob push [remote]`

```bash
blob push D:\backup\repo
```

```
                     Push complete
  Commits synced          3
  Chunks transferred      12
  Chunks skipped (dedup)  5
  Bytes transferred       4.2 MB
  Throughput              180.3 MB/s
  Delta mode              Merkle delta
```

Pushes only commits the remote lacks, and only the chunks those commits need —
computed as a Merkle delta from the newest shared commit. Unreferenced local
objects are never pushed. Repeat pushes transfer nothing.

The remote is created on first push if it does not exist.

A chunk referenced by a commit but missing locally is a hard error pointing at
`fsck`, not a silently incomplete push.

---

## `blob pull [remote]`

```bash
blob pull D:\backup\repo
```

Same delta logic in reverse. **Never touches the working tree** — run
`blob checkout <hash>` afterwards. Merge commits keep both parents.

The remote must already exist; blobtrack will not invent one.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Validation, storage, or integrity failure (including `fsck` problems) |
| 2 | argparse usage error (missing or unknown arguments) |

---

## Remote semantics

`push` and `pull` accept any filesystem path, resolved against the cwd. There
is no remote registry: the default `"origin"` is a **literal relative path**,
not a stored alias, so `blob push` with no argument creates `./origin`. Pass an
explicit path.

Remotes are plain directories with a `.blobtrack` folder. **There is no network
transport, authentication, or encryption** — a remote is a location you already
trust.
