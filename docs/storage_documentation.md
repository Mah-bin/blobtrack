# Storage Subsystem

Three modules, one rule: **storage knows nothing about the CLI.**

| Module | Responsibility |
|---|---|
| `paths.py` | Path normalization and repository containment |
| `local_store.py` | Content-addressed object store |
| `index_db.py` | SQLite metadata: files, commits, chunks, refs |
| `remote_sync.py` | Delta push and pull |

---

## `paths.py` — the security boundary

Every path that enters the database, and every path written to disk during a
checkout, passes through here. Two classes of bug are prevented:

**1. Escaping the repository.** `checkout` writes files. Those paths come from
the database, and a database can arrive from a remote via `pull`. Without
validation, a hostile remote could ask blobtrack to write anywhere on the
filesystem. Absolute paths, drive letters, UNC paths, and `..` segments are all
rejected.

**2. One file under many names.** `add a.bin`, `add ./a.bin`, and
`add sub/../a.bin` are the same file. Normalizing to a single repo-relative
POSIX string keeps the manifest consistent.

The enforced invariant: **a tracked path is always relative to the repository
root and always inside it.**

### `resolve_repo_root(start=None) -> Path | None`

Walks up from `start` (default: cwd) looking for `.blobtrack`. Returns the
repository root or `None`.

### `to_repo_relative(target, repo_root) -> str`

Converts an on-disk path to a repo-relative POSIX string.

```python
to_repo_relative(repo/"sub"/"a.bin", repo)  ->  "sub/a.bin"
```

Raises `UnsafePathError` if the target is outside the repository. Tracking an
external file is refused rather than silently recorded as an absolute path,
because such a snapshot could not be reconstructed from the repository alone.

### `safe_join(repo_root, stored_path) -> Path`

The **write-side guard**. Converts a stored relative path back to an absolute
path, rejecting:

- POSIX absolutes (`/etc/passwd`)
- Windows drive letters (`C:/Windows/win.ini`)
- UNC paths (`\\server\share`)
- any `..` segment

Containment is confirmed after resolution as a second line of defence.

### `resolve_input_path(raw) -> Path`

Resolves a user-supplied CLI path, relative paths against the cwd — what a user
typing `blob add ../foo.bin` expects.

### `is_within(path, root) -> bool`

Convenience predicate.

---

## `local_store.py` — object store

### Layout

```
.blobtrack/objects/
├── .tmp/                          staging for atomic writes
├── ab/abcdef0123456789...         two-character fan-out
└── cd/cdef0123456789...
```

Chunks are named by the SHA-256 of their **uncompressed** bytes and stored
**zstd-compressed**.

**Why fan-out:** a single directory with tens of thousands of entries is slow to
list on most filesystems. Splitting by the first two hex characters keeps each
directory small.

**Legacy compatibility:** releases before the fan-out change stored chunks
directly in `objects/`. Every read path falls back to that location, so old
repositories keep working. Writes always use the canonical layout.

### Constructor

```python
LocalStore(objects_dir)
```

Creates `objects_dir` and `objects_dir/.tmp` if absent.

### Path resolution

| Method | Returns |
|---|---|
| `get_chunk_path(h)` | Canonical fan-out path |
| `get_legacy_chunk_path(h)` | Pre-fan-out flat path |
| `resolve_chunk_path(h)` | Existing path (canonical first, then legacy), or `None` |

### Chunk operations

| Method | Behaviour |
|---|---|
| `has_chunk(h)` | Dedup check across both layouts |
| `store_chunk(h, data)` | Atomic write. `True` if new, `False` if already present |
| `retrieve_chunk(h, verify=False)` | Read stored bytes; `verify=True` also checks content |
| `verify_chunk(h)` | `True` only if present *and* content matches |
| `delete_chunk(h)` | Remove from either layout |
| `list_chunks()` | Every stored hash, both layouts |
| `get_chunk_size(h)` | On-disk size |

#### Atomic writes

`store_chunk` writes to a temp file in `objects/.tmp`, calls `fsync`, then
renames into place. A crash mid-write leaves either the old chunk or the new
one, never a partial file.

`store_chunk` returns `False` if the chunk already exists in **either** layout,
so re-adding to a legacy repository does not duplicate bytes.

#### `verify=True` costs a decompression

The check is not free — it must decompress. It is therefore off by default and
enabled explicitly at the two places where the bytes are about to be trusted:
`checkout` (they become a user file) and `push`/`pull` (they cross a trust
boundary). `fsck` uses the same primitive to verify everything at once.

### Maintenance

| Method | Returns |
|---|---|
| `garbage_collect(active_hashes)` | `(deleted_count, freed_bytes)` — deletes anything not referenced |
| `migrate_layout()` | `(migrated_count, bytes_moved)` — moves legacy flat chunks into fan-out |

`migrate_layout` is idempotent and interruptible: chunks move with
`os.replace`, so a chunk is never absent from both layouts. If both a flat and
a canonical copy exist, the redundant flat one is removed rather than
duplicated.

---

## `index_db.py` — metadata database

SQLite in WAL mode with `synchronous=NORMAL` and foreign keys on.

```python
IndexDB(db_path)   # opens, creating and migrating the schema if needed
init_db(db_path)   # same thing, returns the handle
```

### Schema

```sql
files           path (unique), file_hash, size, last_modified, status, updated_at
commits         commit_hash (pk), parent_hash, message, author,
                timestamp, merkle_root_hash, tree_data
chunks          chunk_hash (pk), size_uncompressed, size_compressed, created_at
chunk_refs      commit_hash, file_path, chunk_hash,
                chunk_offset, chunk_length, chunk_order
refs            ref_name (pk), commit_hash, updated_at
config          key (pk), value
commit_parents  commit_hash, parent_hash, ordinal
```

Every table is created with `IF NOT EXISTS`, so opening an older repository
transparently upgrades it.

`chunk_refs` has `ON DELETE CASCADE` to both `commits` and `chunks`, which
keeps referential integrity automatic.

`commits.parent_hash` holds the **first** parent for backwards compatibility.
`commit_parents` is authoritative when present, which is what allows a merge
commit to have two parents. `get_commit_parents()` falls back to `parent_hash`,
so commits created before the table existed still report ancestry correctly.

### Files

| Method | Purpose |
|---|---|
| `register_file(path, file_hash, size, last_modified, status)` | Insert or update |
| `get_file(path)` | One record, or `None` |
| `list_files(status=None)` | All records sorted by path |
| `remove_file(path)` | Stop tracking. Returns `True` if a row was removed |

`remove_file` only edits the manifest. Chunks referenced by existing commits
are deliberately left alone; `gc` handles reclamation.

### Chunks

`record_chunk`, `record_chunks`, `get_chunk`, `list_chunks`, `count_chunks`.

All writes use `INSERT OR IGNORE` — recording metadata twice is harmless, which
keeps every caller idempotent.

### Commits

| Method | Purpose |
|---|---|
| `save_commit(...)` | Atomically write a commit, its chunk refs, and its parents |
| `get_commit(h)` | One commit; `tree_data` json-decoded |
| `get_latest_commit()` | Newest by timestamp anywhere |
| `list_commits(limit=None, include_tree=True)` | Newest first |
| `get_commit_chunk_refs(h)` | Refs ordered by file then chunk order |
| `get_file_chunks_for_commit(h, path)` | One file's ordered chunks, with sizes |
| `get_commit_file_paths(h)` | Distinct paths in a commit |
| `get_commit_chunk_hashes(h)` | Distinct chunk hashes in reference order |
| `count_commits()` | Commit count |

#### `include_tree=False`

`tree_data` is a serialized Merkle tree — roughly 230 bytes per chunk, so
megabytes for a large repository. Anything that only needs history for
**display** should pass `include_tree=False` and skip parsing it entirely. `log`
and `RemoteSync`'s commit comparison both do.

### Ancestry

| Method | Purpose |
|---|---|
| `get_commit_parents(h)` | Full ordered parent list |
| `is_ancestor(a, b)` | Is `a` anywhere in `b`'s history? Used by `merge` |
| `get_history(h)` | Reachable commits, newest first |
| `delete_commit(h)` | Delete a commit and cascade its refs |

### Refs and config

| Method | Purpose |
|---|---|
| `set_ref` / `get_ref` / `delete_ref` | Branch pointers |
| `list_refs(prefix)` | Refs, optionally filtered |
| `list_branches()` | Branch refs as `{name, commit_hash}` |
| `set_config` / `get_config` | Small settings |
| `get_current_branch()` / `set_current_branch()` | Which branch commits land on |
| `get_branch_head(b)` / `set_branch_head(b, h)` | Branch position |

Helpers `branch_ref_name("feature")` and `short_branch_name("refs/heads/feature")`
convert between short names and full ref names.

### Garbage collection

| Method | Purpose |
|---|---|
| `get_active_chunk_hashes()` | Every hash referenced by any commit |
| `get_orphan_chunks()` | Recorded chunks no commit references |
| `delete_chunk_records(hashes)` | Remove specific chunk rows |

"Active" spans **all** commits, not just the latest, so any restorable version
keeps its chunks.

---

## `remote_sync.py` — delta synchronization

Push and pull are both driven by **commit history**, not by scanning the object
store.

### Why not scan the store

The earlier implementation iterated every chunk the store had ever held and
`stat`ed each one against the remote. Three problems: it scaled with total
history rather than with the change; it pushed objects no commit referenced;
and a repository that had been garbage-collected locally pushed nothing while
still reporting success.

### How the delta is computed

1. Diff the two commit sets to find commits the other side lacks, oldest first
   so parents always arrive before children.
2. Find the **newest commit both sides share** — this guarantees a common
   ancestor exists, which is what makes a Merkle delta well defined.
3. Ask `compute_delta_by_set` for the content delta between that ancestor and
   the newest commit being transferred.
4. Transfer only chunks in that delta, skipping anything already present.

When there is no common ancestor (first sync, or unrelated histories) it falls
back to the union of the transferred commits' references — correct, just less
selective.

The returned stats include `used_merkle_delta`, so callers and users can see
which mode ran.

### Methods

```python
RemoteSync.push(remote_path, local_store, local_db=None,
                delta_chunks=None, commit_hash=None) -> dict
RemoteSync.pull(remote_path, local_store, local_db=None,
                commit_hash=None) -> dict
RemoteSync.init_remote(remote_path) -> (LocalStore, IndexDB)
```

Stats dict: `transferred_chunks`, `transferred_bytes`, `skipped_chunks`,
`commits_synced`, `used_merkle_delta`.

- `_resolve_remote_paths` accepts a repository root or a `.blobtrack` directory.
- `init_remote` creates the remote layout on first push.
- `pull` never touches the working tree; run `blob checkout` afterwards.

### Safety in transfer

- Chunks are read with `verify=True`, so corruption is caught before it
  propagates to another repository.
- A chunk referenced by a commit but absent locally raises `FileNotFoundError`
  with a pointer to `fsck` — **not** a silently incomplete push.
- Commits are synced oldest-first, so a child never arrives before its parent.
- Both methods close the remote database in a `finally` block, so a mid-transfer
  failure cannot leak a connection.

### Parent preservation

`push` and `pull` copy the full parent list, so merge commits keep both parents
on the far side rather than being flattened into a linear chain.
