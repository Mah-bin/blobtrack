"""blobtrack CLI command handlers - integration layer.

Responsibilities, in order of the pipeline:

    init      create a repository
    add       chunk, hash, compress, deduplicate and store a file
    commit    snapshot tracked files as an immutable commit
    log       show history
    checkout  restore a snapshot to the working tree
    rm        stop tracking a path
    fsck      verify repository integrity
    gc        delete chunks no commit references
    migrate   move legacy flat chunks into the fan-out layout
    branch    list or create branches
    switch    move the current branch pointer
    merge     join two branches
    push/pull delta-synchronize with a remote

Shared conventions
------------------
* Errors go to stderr and exit 1. Success goes to stdout and exits 0.
* Nothing is printed until the durable write it describes has succeeded, so a
  rendering failure can never make a failed operation look successful.
* User-supplied text (paths, commit messages) is printed with Rich markup
  disabled. Otherwise a filename like ``a[/]b`` or a message containing
  ``[/red]`` would be parsed as a style tag and raise mid-operation.
"""

from __future__ import annotations

import pathlib
import shutil
import sys
from collections import defaultdict

try:
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
    from rich.table import Table

    console = Console()
    error_console = Console(stderr=True)
    HAS_RICH = True
except ImportError:  # pragma: no cover - exercised only without rich
    console = None
    error_console = None
    Table = None
    HAS_RICH = False

from blobtrack.core.merkle_tree import deserialize_tree, serialize_tree
from blobtrack.storage.index_db import (
    DEFAULT_BRANCH,
    IndexDB,
    branch_ref_name,
    short_branch_name,
)
from blobtrack.storage.paths import (
    UnsafePathError,
    resolve_input_path,
    resolve_repo_root,
    safe_join,
    to_repo_relative,
)

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _print_success(msg: str) -> None:
    """Print a success line to stdout. Never interprets Rich markup."""
    if HAS_RICH and console:
        console.print(msg, markup=False, highlight=False)
    else:
        print(msg)


def _print_error(msg: str) -> None:
    """Print an error to stderr. Never interprets Rich markup."""
    if HAS_RICH and error_console:
        error_console.print(f"Error: {msg}", markup=False, highlight=False)
    else:
        print(f"Error: {msg}", file=sys.stderr)


def _print_plain(msg: str, style: str = "") -> None:
    """Print a dim/helper line to stdout without markup interpretation."""
    if HAS_RICH and console:
        console.print(msg, style=style, markup=False, highlight=False)
    else:
        print(msg)


def _fail(msg: str) -> None:
    """Report an error and exit non-zero."""
    _print_error(msg)
    sys.exit(1)


def _format_bytes(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes} bytes"


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s ({seconds:.1f}s)"


def _warn_backend_once() -> None:
    """Warn once per process when content-defined chunking is pure Python.

    fastcdc's compiled accelerator is unavailable on some interpreters. The
    fallback runs roughly an order of magnitude slower, and it dominates the
    whole pipeline, so silently getting it would make every performance
    number measured on this machine inexplicable.
    """
    from blobtrack.core.chunker import CDC_BACKEND

    if CDC_BACKEND == "native" or getattr(_warn_backend_once, "_warned", False):
        return
    _warn_backend_once._warned = True  # type: ignore[attr-defined]
    _print_plain(
        "warning: fastcdc is running in pure-Python mode; chunking is the "
        "bottleneck and will be much slower. Using CPython 3.10-3.13 gives "
        "the compiled accelerator.",
        style="yellow",
    )


# ---------------------------------------------------------------------------
# Repository plumbing
# ---------------------------------------------------------------------------


def _find_repo_root(start: pathlib.Path | None = None) -> pathlib.Path | None:
    """Backwards-compatible alias for resolve_repo_root."""
    return resolve_repo_root(start)


def _open_repo_storage(
    cwd: pathlib.Path | None = None,
):
    """Open (IndexDB, LocalStore) for the repository containing cwd."""
    repo_root = resolve_repo_root(cwd)
    if repo_root is None:
        _fail("not a blobtrack repository (or any parent up to root). Run 'blob init' first.")

    db_path = repo_root / ".blobtrack" / "index.db"
    objects_dir = repo_root / ".blobtrack" / "objects"

    try:
        from blobtrack.storage.index_db import IndexDB as _IndexDB
        from blobtrack.storage.local_store import LocalStore

        return repo_root, _IndexDB(db_path), LocalStore(objects_dir)
    except Exception as exc:
        _fail(f"failed to open repository storage: {exc}")


def _update_progress_bar(progress, task_id, **kwargs) -> None:
    """Best-effort progress update; never let UI break the operation."""
    if progress is None or task_id is None:
        return
    try:
        progress.update(task_id, **kwargs)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def cmd_init(cwd: pathlib.Path | None = None) -> None:
    """Create a new blobtrack repository.

    Creates ``.blobtrack/objects/``, ``.blobtrack/commits/`` and an initialized
    ``.blobtrack/index.db``. Refuses to touch an existing repository and rolls
    back completely if any step fails, so a half-created repository is never
    left behind.
    """
    base = pathlib.Path.cwd() if cwd is None else pathlib.Path(cwd)
    base = base.resolve()
    blobtrack_dir = base / ".blobtrack"

    if blobtrack_dir.exists():
        _fail(f"repository already initialized in {blobtrack_dir}")

    if not base.is_dir():
        _fail(f"not a directory: {base}")

    objects_dir = blobtrack_dir / "objects"
    commits_dir = blobtrack_dir / "commits"

    try:
        for directory in (blobtrack_dir, objects_dir, commits_dir):
            directory.mkdir(mode=0o700, exist_ok=False)
            try:
                directory.chmod(0o700)
            except Exception:
                pass

        # Build the schema through IndexDB so init and normal use agree.
        from blobtrack.storage.index_db import IndexDB

        db = IndexDB(blobtrack_dir / "index.db")
        db.close()

    except FileExistsError:
        _fail(f"repository already initialized in {blobtrack_dir}")
    except Exception as exc:
        if blobtrack_dir.exists():
            try:
                shutil.rmtree(blobtrack_dir)
            except Exception:
                pass
        _fail(f"failed to initialize repository: {exc}")

    _print_success(f"Initialized empty blobtrack repository in {blobtrack_dir}")


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def cmd_add(filepath: str) -> None:
    """Chunk, compress, deduplicate and store a file.

    Streams the file through content-defined chunking, hashing and compressing
    each chunk in parallel. Chunks already in the store are reused rather than
    rewritten, which is what makes repeated adds of similar large files cheap.
    """
    repo_root = resolve_repo_root()
    if repo_root is None:
        _fail("not a blobtrack repository (or any parent up to root). Run 'blob init' first.")

    try:
        target = resolve_input_path(filepath)
    except Exception as exc:
        _fail(f"invalid path: {exc}")

    if not target.exists():
        _fail(f"file not found: {filepath}")
    if not target.is_file():
        _fail(f"not a file: {filepath}")

    try:
        rel_posix = to_repo_relative(target, repo_root)
    except UnsafePathError as exc:
        _fail(str(exc))

    try:
        from blobtrack.core.chunker import chunk_file_streaming
        from blobtrack.core.hasher import hash_file_streaming, process_chunks

        repo_root, index_db, local_store = _open_repo_storage()
    except SystemExit:
        raise
    except Exception as exc:
        _fail(f"failed to open repository storage: {exc}")

    try:
        file_size = target.stat().st_size
        last_modified = target.stat().st_mtime
    except OSError as exc:
        index_db.close()
        _fail(f"cannot stat file: {exc}")

    import time as _time

    started = _time.time()
    _warn_backend_once()

    try:
        # add always compresses: every chunk may need to be written.
        processed_iter = process_chunks(
            chunk_file_streaming(str(target)), batch_size=16, max_workers=8
        )
    except Exception as exc:
        index_db.close()
        _fail(f"failed to chunk file: {exc}")

    new_chunks = 0
    reused_chunks = 0
    total_chunks = 0
    total_uncompressed = 0
    total_compressed = 0

    progress = task_id = None
    if HAS_RICH and console and file_size > 10 * 1024 * 1024:
        try:
            progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TextColumn(" | "),
                TextColumn("{task.fields[chunk_info]}"),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                console=console,
            )
            progress.start()
            task_id = progress.add_task(
                f"Adding {rel_posix}...", total=file_size, chunk_info="0 chunks"
            )
        except Exception:
            progress = None
            task_id = None

    try:
        for pchunk in processed_iter:
            total_chunks += 1
            total_uncompressed += pchunk.length
            total_compressed += len(pchunk.compressed_data)

            if local_store.has_chunk(pchunk.hash):
                reused_chunks += 1
            else:
                local_store.store_chunk(pchunk.hash, pchunk.compressed_data)
                new_chunks += 1

            index_db.record_chunk(
                chunk_hash=pchunk.hash,
                size_uncompressed=pchunk.length,
                size_compressed=len(pchunk.compressed_data),
            )

            _update_progress_bar(
                progress,
                task_id,
                advance=pchunk.length,
                chunk_info=(f"{total_chunks} chunks ({new_chunks} new, {reused_chunks} reused)"),
            )
    except ValueError as exc:
        _fail(str(exc))
    except Exception as exc:
        _fail(f"failed to process chunks: {exc}")
    finally:
        if progress:
            try:
                progress.stop()
            except Exception:
                pass
        try:
            index_db.close()
        except Exception:
            pass

    if total_chunks == 0:
        _fail(f"file is empty or produced no chunks: {filepath}")

    try:
        index_db = _open_repo_storage()[1]
        index_db.register_file(
            path=rel_posix,
            file_hash=hash_file_streaming(str(target)),
            size=file_size,
            last_modified=last_modified,
            status="tracked",
        )
    except Exception as exc:
        _fail(f"failed to register file in database: {exc}")
    finally:
        try:
            index_db.close()
        except Exception:
            pass

    dedup_pct = (reused_chunks / total_chunks * 100) if total_chunks else 0.0
    _print_success(
        f"Added '{rel_posix}' -> {total_chunks} chunks "
        f"({new_chunks} new, {reused_chunks} reused, {dedup_pct:.1f}% dedup) "
        f"[{total_uncompressed} -> {total_compressed} bytes compressed] "
        f"in {_format_duration(_time.time() - started)}"
    )


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------


def _build_commit_tree(
    index_db: IndexDB,
    local_store,
    repo_root: pathlib.Path,
) -> tuple[list[str] | None, list[dict], int, int, int]:
    """Chunk every tracked file and return the commit's data.

    Missing tracked files are a hard error. Silently skipping them would let a
    commit quietly drop a file from history while reporting success, which is
    the single worst failure mode a version control system can have.

    Returns:
        (combined_hashes, file_chunk_mappings, files_count, chunks_count,
         new_chunks_stored)

    Raises:
        FileNotFoundError: a tracked file is missing from disk.
    """
    from blobtrack.core.chunker import chunk_file_streaming
    from blobtrack.core.hasher import process_chunks

    tracked = index_db.list_files(status="tracked") or index_db.list_files()
    if not tracked:
        return None, [], 0, 0, 0

    tracked_sorted = sorted(tracked, key=lambda f: f["path"])

    missing = []
    for record in tracked_sorted:
        candidate = repo_root / pathlib.Path(record["path"])
        if not candidate.is_file():
            missing.append(record["path"])

    if missing:
        raise FileNotFoundError(
            "tracked file(s) missing from the working tree: "
            + ", ".join(missing)
            + "\n  Restore them, or run 'blob rm <path>' to stop tracking them."
        )

    def needs_payload(chunk_hash: str) -> bool:
        """Only chunks we do not already have need compressing."""
        return not local_store.has_chunk(chunk_hash)

    combined_hashes: list[str] = []
    file_chunk_mappings: list[dict] = []
    files_count = 0
    chunks_count = 0
    new_stored = 0

    total_bytes = 0
    for record in tracked_sorted:
        disk_path = repo_root / pathlib.Path(record["path"])
        try:
            total_bytes += disk_path.stat().st_size
        except OSError:
            pass

    progress = task_id = None
    if HAS_RICH and console and total_bytes > 10 * 1024 * 1024:
        try:
            progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TextColumn(" | "),
                TextColumn("{task.fields[info]}"),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                console=console,
            )
            progress.start()
            task_id = progress.add_task(
                "Committing...", total=total_bytes, info=f"0/{len(tracked_sorted)} files"
            )
        except Exception:
            progress = None
            task_id = None

    processed_bytes = 0
    try:
        for record in tracked_sorted:
            disk_path = repo_root / pathlib.Path(record["path"])
            stream = chunk_file_streaming(str(disk_path))

            for pchunk in process_chunks(
                stream, batch_size=16, max_workers=8, needs_payload=needs_payload
            ):
                combined_hashes.append(pchunk.hash)

                # Persist anything we did not already have, so the commit we
                # are about to write is always fully backed by real objects.
                if pchunk.has_payload:
                    local_store.store_chunk(pchunk.hash, pchunk.compressed_data)
                    new_stored += 1
                    index_db.record_chunk(
                        chunk_hash=pchunk.hash,
                        size_uncompressed=pchunk.length,
                        size_compressed=len(pchunk.compressed_data),
                    )

                file_chunk_mappings.append(
                    {
                        "file_path": record["path"],
                        "chunk_hash": pchunk.hash,
                        "chunk_offset": pchunk.offset,
                        "chunk_length": pchunk.length,
                        "chunk_order": pchunk.index,
                        "size_uncompressed": pchunk.length,
                        "size_compressed": len(pchunk.compressed_data),
                    }
                )

                chunks_count += 1
                processed_bytes += pchunk.length
                _update_progress_bar(
                    progress,
                    task_id,
                    completed=processed_bytes,
                    info=f"{files_count + 1}/{len(tracked_sorted)} files",
                )

            files_count += 1
            _update_progress_bar(
                progress,
                task_id,
                info=f"{files_count}/{len(tracked_sorted)} files "
                f"| {chunks_count} chunks ({new_stored} new)",
            )
    finally:
        if progress:
            try:
                progress.stop()
            except Exception:
                pass

    return combined_hashes, file_chunk_mappings, files_count, chunks_count, new_stored


def cmd_commit(message: str) -> None:
    """Snapshot every tracked file as an immutable commit.

    The Merkle root is built over the ordered chunk hashes of every tracked
    file, so the root identifies the exact repository state. The parent is the
    previous commit on the current branch.
    """
    from blobtrack.core.differ import compute_delta_by_set
    from blobtrack.core.merkle_tree import build_tree

    if not message or not message.strip():
        _fail('commit message cannot be empty. Use -m "message"')

    repo_root, index_db, local_store = _open_repo_storage()
    import time

    try:
        started = time.time()
        _warn_backend_once()

        try:
            (
                combined_hashes,
                file_chunk_mappings,
                files_count,
                chunks_count,
                new_stored,
            ) = _build_commit_tree(index_db, local_store, repo_root)
        except FileNotFoundError as exc:
            _fail(str(exc))

        if not combined_hashes:
            _fail(
                "no chunks to commit. Run 'blob add <file>' first, or "
                "'blob rm <path>' if the tracked file is gone."
            )

        new_tree = build_tree(combined_hashes)
        if new_tree is None:
            _fail("failed to build Merkle tree")

        merkle_root = new_tree.hash
        tree_data = serialize_tree(new_tree)

        branch = index_db.get_current_branch()
        parent_hash = index_db.get_branch_head(branch) or index_db.get_latest_commit_commit_hash()

        parent_tree = None
        if parent_hash:
            parent_commit = index_db.get_commit(parent_hash)
            if parent_commit and parent_commit.get("tree_data"):
                tree_payload = parent_commit["tree_data"]
                if not isinstance(tree_payload, str):
                    import json as _json

                    tree_payload = _json.dumps(tree_payload)
                try:
                    parent_tree = deserialize_tree(tree_payload)
                except Exception:
                    parent_tree = None

        timestamp = time.time()
        commit_hash = _compute_commit_hash(merkle_root, message, timestamp, parent_hash)

        # Content-based delta. A positional walk under-reports shared content
        # whenever an edit shifts chunk positions, which is the normal case for
        # CDC, so the set-based diff is the honest number to show.
        delta_info = ""
        if parent_tree is not None:
            delta = compute_delta_by_set(parent_tree, new_tree)
            delta_info = (
                f" | delta: +{len(delta['added'])} "
                f"-{len(delta['removed'])} ={len(delta['unchanged'])}"
            )

        # Persist first, print second: output must never precede the durable
        # write it claims to describe.
        index_db.save_commit(
            commit_hash=commit_hash,
            message=message,
            parent_hash=parent_hash,
            author=None,
            timestamp=timestamp,
            merkle_root_hash=merkle_root,
            tree_data=tree_data,
            file_chunk_mappings=file_chunk_mappings,
            parents=[parent_hash] if parent_hash else [],
        )
        index_db.set_branch_head(branch, commit_hash)
        if not index_db.get_ref(branch_ref_name(DEFAULT_BRANCH)):
            index_db.set_branch_head(DEFAULT_BRANCH, commit_hash)

        elapsed = time.time() - started
        _print_success(
            f"Committed {commit_hash[:12]} - {files_count} file(s), "
            f"{chunks_count} chunks ({new_stored} new), "
            f'root {merkle_root[:12]}...{delta_info} - "{message}"'
        )
        parent_note = f"{parent_hash[:12]}" if parent_hash else "-"
        _print_plain(
            f"branch {short_branch_name(branch)} | "
            f"parent {parent_note} -> {commit_hash[:12]} in {elapsed:.1f}s",
            style="dim",
        )
    finally:
        try:
            index_db.close()
        except Exception:
            pass


def _compute_commit_hash(
    merkle_root: str, message: str, timestamp: float, parent_hash: str | None
) -> str:
    """Content-derived commit id.

    Includes the timestamp so two identical snapshots produce distinct
    commits, and the parent so the chain is tamper-evident.
    """
    from blobtrack.core.hasher import hash_bytes

    payload = f"{merkle_root}:{message}:{timestamp}:{parent_hash or ''}"
    return hash_bytes(payload.encode("utf-8"))


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


def cmd_log() -> None:
    """Show commit history, newest first."""
    repo_root, index_db, _ = _open_repo_storage()

    try:
        branch = index_db.get_current_branch()
        head = index_db.get_branch_head(branch)
        # include_tree=False: history display does not need the (potentially
        # multi-megabyte) Merkle trees, and parsing them is pure waste.
        commits = index_db.list_commits(include_tree=False)
    finally:
        try:
            index_db.close()
        except Exception:
            pass

    if not commits:
        _print_success("No commits yet. Use 'blob commit -m \"message\"' to create one.")
        return

    import datetime

    def _fmt(ts) -> str:
        try:
            return (
                datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
                if ts
                else ""
            )
        except Exception:
            return str(ts or "")

    if HAS_RICH and Table is not None:
        from rich.text import Text

        table = Table(title=f"Commit history ({len(commits)} commits)", show_lines=True)
        table.add_column("Hash", style="cyan", no_wrap=True)
        table.add_column("Message", style="white")
        table.add_column("Author", style="green")
        table.add_column("Date", style="dim")
        table.add_column("Parent", style="yellow")
        for commit in commits:
            # Text(...) renders literally; a bare str would be parsed for
            # markup and a message like "fix [/red]" would raise here.
            table.add_row(
                commit["commit_hash"][:12],
                Text((commit.get("message") or "")[:50]),
                Text(commit.get("author") or "-"),
                Text(_fmt(commit.get("timestamp"))),
                Text((commit.get("parent_hash") or "")[:12] or "-"),
            )
        console.print(table)
    else:
        for commit in commits:
            print(f"commit {commit['commit_hash']}")
            print(f"  Message: {commit.get('message', '')}")
            print(f"  Author: {commit.get('author') or '-'}")
            print(f"  Date: {_fmt(commit.get('timestamp'))}")
            print(f"  Parent: {commit.get('parent_hash') or '-'}")
            print(f"  Merkle: {(commit.get('merkle_root_hash') or '')[:12]}")
            print()

    _print_plain(
        f"Displayed {len(commits)} commit(s) | branch "
        f"{short_branch_name(branch)} @ {head[:12] if head else '-'}",
        style="dim",
    )


# ---------------------------------------------------------------------------
# checkout
# ---------------------------------------------------------------------------


def _resolve_commit_reference(index_db: IndexDB, reference: str) -> str:
    """Resolve a branch name or (possibly abbreviated) commit hash.

    Branch names win over hashes, matching what a user typing ``blob checkout
    main`` expects.

    Raises:
        KeyError: no branch and no unique commit matches.
    """
    ref_value = index_db.get_ref(branch_ref_name(reference))
    if ref_value:
        return ref_value

    ref_value = index_db.get_ref(reference)
    if ref_value:
        return ref_value

    exact = index_db.get_commit(reference)
    if exact is not None:
        return exact["commit_hash"]

    matches = [
        c["commit_hash"]
        for c in index_db.list_commits(include_tree=False)
        if c["commit_hash"].startswith(reference)
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise KeyError(f"ambiguous commit prefix '{reference}' matches {len(matches)} commits")
    raise KeyError(f"commit not found: {reference}")


def _restore_commit(
    index_db: IndexDB,
    local_store,
    repo_root: pathlib.Path,
    commit_hash: str,
) -> tuple[int, int, int]:
    """Write every file in a commit back to the working tree.

    Each restored file is verified chunk-by-chunk against its SHA-256 before
    being moved into place, so a corrupt object is reported instead of being
    silently written out.

    Returns:
        (files_restored, chunks_written, bytes_written)
    """
    import tempfile

    refs = index_db.get_commit_chunk_refs(commit_hash)
    if not refs:
        _fail(f"commit {commit_hash[:12]} has no file chunk references")

    grouped: dict[str, list[dict]] = defaultdict(list)
    for ref in refs:
        grouped[ref["file_path"]].append(ref)
    for file_path in grouped:
        grouped[file_path] = sorted(grouped[file_path], key=lambda r: r["chunk_order"])

    files_restored = 0
    total_bytes = 0
    chunks_written = 0

    for file_path, chunk_refs in sorted(grouped.items()):
        try:
            out_path = safe_join(repo_root, file_path)
        except UnsafePathError as exc:
            _fail(f"unsafe path in commit metadata: {exc}")

        out_path.parent.mkdir(parents=True, exist_ok=True)

        progress = task_id = None
        if HAS_RICH and len(chunk_refs) > 32:
            try:
                progress = Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TaskProgressColumn(),
                    TimeElapsedColumn(),
                    console=console,
                    transient=True,
                )
                progress.start()
                task_id = progress.add_task(f"Restoring {file_path}...", total=len(chunk_refs))
            except Exception:
                progress = None
                task_id = None

        fd, tmp_name = tempfile.mkstemp(
            dir=str(out_path.parent), prefix=".blob_checkout_", suffix=".tmp"
        )
        try:
            with open(fd, "wb") as out_f:
                for ref in chunk_refs:
                    chunk_hash = ref["chunk_hash"]
                    try:
                        # verify=True: the bytes are about to become a user file.
                        compressed = local_store.retrieve_chunk(chunk_hash, verify=True)
                    except FileNotFoundError:
                        _fail(
                            f"required chunk {chunk_hash[:12]} missing for "
                            f"{file_path} (commit {commit_hash[:12]}). "
                            f"Run 'blob fsck' for details."
                        )
                    except Exception as exc:
                        _fail(f"failed to read chunk {chunk_hash[:12]}: {exc}")

                    from blobtrack.core.packer import decompress

                    try:
                        raw = decompress(compressed)
                    except Exception as exc:
                        _fail(f"failed to decompress chunk {chunk_hash[:12]}: {exc}")

                    expected_len = ref.get("chunk_length")
                    if expected_len and len(raw) != expected_len:
                        _fail(
                            f"chunk length mismatch for {chunk_hash[:12]}: "
                            f"expected {expected_len}, got {len(raw)}"
                        )

                    out_f.write(raw)
                    total_bytes += len(raw)
                    chunks_written += 1
                    _update_progress_bar(progress, task_id, advance=1)

            actual_size = pathlib.Path(tmp_name).stat().st_size
            expected_size = sum(r.get("chunk_length", 0) or 0 for r in chunk_refs)
            if expected_size and actual_size != expected_size:
                _fail(
                    f"reconstructed size mismatch for {file_path}: {actual_size} vs {expected_size}"
                )

            pathlib.Path(tmp_name).replace(out_path)
            files_restored += 1
        except SystemExit:
            raise
        except Exception as exc:
            try:
                pathlib.Path(tmp_name).unlink(missing_ok=True)
            except Exception:
                pass
            _fail(f"failed to checkout {file_path}: {exc}")
        finally:
            if progress:
                try:
                    progress.stop()
                except Exception:
                    pass

    return files_restored, chunks_written, total_bytes


def cmd_checkout(commit_hash: str) -> None:
    """Restore a commit's files into the working tree.

    Accepts a full hash, an abbreviated hash, or a branch name. Tracked files
    present in the commit are rewritten; unrelated untracked files are left
    alone. Every chunk is verified before it is written.
    """
    import re

    if not commit_hash or not commit_hash.strip():
        _fail("commit hash cannot be empty")

    reference = commit_hash.strip()
    # A reference is either a branch name or hex. Anything else is a typo.
    looks_like_hash = bool(re.fullmatch(r"[0-9a-fA-F]{6,64}", reference))
    if not looks_like_hash and not re.fullmatch(r"[A-Za-z0-9._/-]{1,255}", reference):
        _fail(f"invalid commit reference: {commit_hash}")

    repo_root, index_db, local_store = _open_repo_storage()

    try:
        try:
            target_hash = _resolve_commit_reference(index_db, reference)
        except KeyError as exc:
            _fail(str(exc).strip("'"))

        commit = index_db.get_commit(target_hash)
        if commit is None:
            _fail(f"commit not found: {reference}")

        files_restored, chunks_written, total_bytes = _restore_commit(
            index_db, local_store, repo_root, target_hash
        )
    finally:
        try:
            index_db.close()
        except Exception:
            pass

    _print_success(
        f"Checked out {target_hash[:12]} - restored {files_restored} file(s), "
        f"{chunks_written} chunks, {total_bytes} bytes (verified)"
    )
    _print_plain(
        f'Commit: "{commit.get("message", "")}" parent {commit.get("parent_hash") or "-"}',
        style="dim",
    )


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------


def cmd_rm(filepath: str, recursive: bool = False) -> None:
    """Stop tracking one path, or every path under a directory.

    Removes the path from the manifest. Existing commits are untouched -- this
    is how you record that a tracked file was intentionally deleted rather than
    having it silently disappear from history on the next commit.

    Works on paths that are already gone from disk, which is the usual reason to
    run it. A directory argument removes every tracked path beneath it, so
    ``blob rm Documents`` matches ``blob rm Documents/my report.docx``.

    Chunks that no commit references become reclaimable with ``blob gc``.
    """
    repo_root, index_db, _ = _open_repo_storage()

    try:
        targets = _resolve_rm_targets(index_db, repo_root, filepath, recursive)

        removed = []
        for rel_posix in targets:
            if index_db.remove_file(rel_posix):
                removed.append(rel_posix)

        if not removed:
            _fail(f"not tracked: {filepath}")

        still_on_disk = [p for p in removed if (repo_root / pathlib.Path(p)).exists()]
        remaining = len(index_db.list_files())
    finally:
        try:
            index_db.close()
        except Exception:
            pass

    for rel_posix in removed:
        suffix = "" if rel_posix in still_on_disk else " (file was already absent from disk)"
        _print_success(f"Removed '{rel_posix}' from tracking{suffix}")

    if remaining:
        _print_plain(
            "Existing commits still contain these files; run 'blob gc' to "
            "reclaim chunks no commit references.",
            style="dim",
        )
    else:
        _print_plain(
            "No files are tracked now. 'blob commit' needs at least one tracked "
            "file, so add something before committing again.",
            style="dim",
        )


def _resolve_rm_targets(
    index_db: IndexDB, repo_root: pathlib.Path, filepath: str, recursive: bool
) -> list[str]:
    """Work out which repo-relative paths a `blob rm` invocation refers to.

    Handles three cases:

    1. the file is on disk inside the repo -> use its repo-relative path;
    2. it is already gone -> match on the normalized relative path;
    3. it names a directory -> every tracked path beneath it, which requires
       ``recursive`` so that ``blob rm Documents`` cannot quietly untrack a
       whole tree by accident.
    """
    try:
        target = resolve_input_path(filepath)
    except Exception as exc:
        _fail(f"invalid path: {exc}")

    try:
        rel_posix = to_repo_relative(target, repo_root)
    except UnsafePathError:
        # Outside the repository, or already deleted. Fall back to treating the
        # argument as a repo-relative path so `rm` still works on missing files.
        rel_posix = pathlib.Path(filepath).as_posix()

    # Normalize separators and collapse "." without resolving symlinks.
    parts = [p for p in rel_posix.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        _fail(f"refusing to use path that escapes the repository: {filepath}")
    rel_posix = "/".join(parts)

    if not rel_posix:
        _fail(f"not tracked: {filepath}")

    if index_db.get_file(rel_posix) is not None:
        return [rel_posix]

    # Not tracked as a file: is it a directory we should expand?
    tracked = [record["path"] for record in index_db.list_files()]
    prefix = rel_posix + "/"
    descendants = [p for p in tracked if p.startswith(prefix)]

    if descendants:
        if not recursive:
            _fail(
                f"'{rel_posix}' is a directory with {len(descendants)} tracked "
                f"file(s). Use 'blob rm -r {rel_posix}' to untrack them."
            )
        return sorted(descendants)

    raise SystemExit(1)


# ---------------------------------------------------------------------------
# fsck
# ---------------------------------------------------------------------------


def cmd_fsck() -> None:
    """Verify repository integrity.

    Checks four things:

    1. every chunk referenced by a commit exists in the object store;
    2. every stored chunk decompresses and hashes to the name it is filed under;
    3. how many chunks are unreferenced (reclaimable via ``gc``);
    4. whether chunks exist on disk that the database has never heard of.

    Exits 1 if anything is wrong, so it is usable in CI or a pre-push hook.
    """
    from blobtrack.core.integrity import scan_chunks

    repo_root, index_db, local_store = _open_repo_storage()

    try:
        _print_plain(f"Checking repository at {repo_root}", style="dim")

        active = index_db.get_active_chunk_hashes()
        recorded = {c["chunk_hash"] for c in index_db.list_chunks()}
        stored = set(local_store.list_chunks())

        missing, corrupt = scan_chunks(local_store, sorted(active))

        unknown = stored - recorded
        unrecorded_active = active - recorded

        orphans = index_db.get_orphan_chunks()

        total_stored_bytes = 0
        for chunk_hash in stored:
            try:
                total_stored_bytes += local_store.get_chunk_size(chunk_hash)
            except OSError:
                pass

        print()
        _print_plain(f"Commits:            {index_db.count_commits()}")
        _print_plain(f"Referenced chunks:  {len(active)}")
        _print_plain(f"Stored chunks:      {len(stored)}")
        _print_plain(f"Stored size:        {_format_bytes(total_stored_bytes)}")
        _print_plain(f"Unreferenced:       {len(orphans)} (reclaimable with 'blob gc')")
        print()

        ok = True
        if missing:
            ok = False
            _print_error(f"{len(missing)} referenced chunk(s) missing from the object store:")
            for chunk_hash in missing[:10]:
                print(f"    missing  {chunk_hash}")
            if len(missing) > 10:
                print(f"    ... and {len(missing) - 10} more")
            _print_plain(
                "  Recover with 'blob push' from a clone that still has them.",
                style="dim",
            )

        if corrupt:
            ok = False
            _print_error(f"{len(corrupt)} stored chunk(s) failed integrity verification:")
            for chunk_hash in corrupt[:10]:
                print(f"    corrupt  {chunk_hash}")
            if len(corrupt) > 10:
                print(f"    ... and {len(corrupt) - 10} more")
            _print_plain(
                "  These objects are damaged. Re-add the affected files from a known-good copy.",
                style="dim",
            )

        if unrecorded_active:
            _print_plain(
                f"note: {len(unrecorded_active)} referenced chunk(s) have no "
                f"metadata row; 'blob fsck --repair' would add them.",
                style="yellow",
            )

        if unknown:
            _print_plain(
                f"note: {len(unknown)} object(s) on disk are unknown to the "
                f"database; 'blob gc' will remove them if unreferenced.",
                style="yellow",
            )

        print()
        if ok:
            _print_success(
                f"fsck OK - {len(active)} referenced chunk(s) verified, no corruption found."
            )
        else:
            _fail(
                f"fsck found {len(missing)} missing and {len(corrupt)} corrupt "
                f"chunk(s). The repository is not intact."
            )
    finally:
        try:
            index_db.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# gc
# ---------------------------------------------------------------------------


def cmd_gc(dry_run: bool = False) -> None:
    """Delete chunks that no commit references.

    Uses the set of chunks referenced by *any* commit as the definition of
    "alive", so anything a commit could ever need is preserved. Only truly
    unreferenced objects are removed.
    """
    repo_root, index_db, local_store = _open_repo_storage()

    try:
        active = index_db.get_active_chunk_hashes()
        stored_before = local_store.list_chunks()
        db_orphans = index_db.get_orphan_chunks()

        if dry_run:
            reclaimable_fs = [c for c in stored_before if c not in active]
            reclaimable_bytes = 0
            for chunk_hash in reclaimable_fs:
                try:
                    reclaimable_bytes += local_store.get_chunk_size(chunk_hash)
                except OSError:
                    pass
            print()
            _print_plain(
                f"Dry run: {len(reclaimable_fs)} object(s) would be deleted, "
                f"{_format_bytes(reclaimable_bytes)} freed."
            )
            _print_plain(
                f"Active chunks preserved: {len(active)} | "
                f"DB rows that would be removed: {len(db_orphans)}",
                style="dim",
            )
            return

        deleted_fs, freed_bytes = local_store.garbage_collect(active)
        deleted_db = index_db.delete_chunk_records(index_db.get_orphan_chunks())

        print()
        if deleted_fs == 0 and deleted_db == 0:
            _print_success(
                f"No orphan chunks found - {len(active)} active, "
                f"{len(stored_before)} stored, 0 bytes freed."
            )
        else:
            _print_success(
                f"Deleted {deleted_fs} orphan chunk(s) from objects and "
                f"{deleted_db} DB record(s); freed {_format_bytes(freed_bytes)}. "
                f"Active chunks preserved: {len(active)}."
            )
    except Exception as exc:
        _fail(f"garbage collection failed: {exc}")
    finally:
        try:
            index_db.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------


def cmd_migrate(dry_run: bool = False) -> None:
    """Move legacy flat-layout chunks into the fan-out layout.

    Repositories created before the fan-out change stored chunks directly in
    ``.blobtrack/objects/``. Reads already fall back to that location, so this
    is purely housekeeping -- it makes large directories faster to list and
    removes the ambiguity.
    """
    repo_root, index_db, local_store = _open_repo_storage()

    try:
        before = len(local_store.list_chunks())
        if dry_run:
            legacy = [
                entry.name
                for entry in (repo_root / ".blobtrack" / "objects").iterdir()
                if entry.is_file() and not entry.name.startswith(".")
            ]
            print()
            _print_plain(
                f"Dry run: {len(legacy)} legacy chunk(s) would be moved into the fan-out layout."
            )
            return

        migrated, moved_bytes = local_store.migrate_layout()
        after = len(local_store.list_chunks())
    finally:
        try:
            index_db.close()
        except Exception:
            pass

    print()
    if migrated == 0:
        _print_success(f"Nothing to migrate - all {after} chunk(s) already use the fan-out layout.")
    else:
        _print_success(
            f"Migrated {migrated} chunk(s) into the fan-out layout "
            f"({_format_bytes(moved_bytes)}); {after} chunk(s) total, "
            f"verified {before} before."
        )


# ---------------------------------------------------------------------------
# branch / switch / merge
# ---------------------------------------------------------------------------


def cmd_branch(name: str | None = None, delete: bool = False) -> None:
    """List branches, or create one at the current commit."""
    repo_root, index_db, _ = _open_repo_storage()

    try:
        current = short_branch_name(index_db.get_current_branch())

        if delete:
            if not name:
                _fail("branch name required: blob branch -d <name>")
            if name == current:
                _fail(f"cannot delete '{name}': it is the current branch")
            if not index_db.delete_ref(branch_ref_name(name)):
                _fail(f"branch not found: {name}")
            _print_success(f"Deleted branch {name}")
            return

        if name is None:
            branches = index_db.list_branches()
            if not branches:
                _print_success(f"No branches yet. Current branch: {current} (no commits).")
                return
            print()
            for branch in branches:
                marker = "*" if branch["name"] == current else " "
                head = branch["commit_hash"]
                commit = index_db.get_commit(head) if head else None
                subject = (commit or {}).get("message", "")
                short = head[:12] if head else "-"
                print(f" {marker} {branch['name']:<20} {short}  {subject[:50]}")
            print()
            _print_plain(f"* current branch ({current})", style="dim")
            return

        if index_db.get_ref(branch_ref_name(name)):
            _fail(f"branch already exists: {name}")

        head = index_db.get_branch_head(current) or index_db.get_latest_commit_commit_hash()
        if not head:
            _fail("cannot create a branch before the first commit")

        index_db.set_branch_head(name, head)
    finally:
        try:
            index_db.close()
        except Exception:
            pass

    _print_success(f"Created branch '{name}' at {head[:12]}")


def cmd_switch(branch: str) -> None:
    """Point the current branch at another branch's commit."""
    repo_root, index_db, _ = _open_repo_storage()

    try:
        target = index_db.get_ref(branch_ref_name(branch))
        if not target:
            _fail(f"branch not found: {branch}")

        previous = short_branch_name(index_db.get_current_branch())
        index_db.set_current_branch(branch)
        head = target
    finally:
        try:
            index_db.close()
        except Exception:
            pass

    _print_success(f"Switched from {previous} to {branch} (now at {head[:12]})")
    _print_plain(
        "Working tree unchanged. Run 'blob checkout <branch>' to restore files.",
        style="dim",
    )


def cmd_merge(branch: str) -> None:
    """Merge another branch into the current one.

    This is a content-level union, appropriate for large binaries: files
    present on only one side are taken from that side, and files changed on
    both sides to *different* content are reported as conflicts rather than
    silently resolved, because there is no sensible line-level merge for a
    20 GB video.

    Fast-forwards when the current branch is an ancestor, so the common case
    stays linear.
    """
    from blobtrack.core.merkle_tree import build_tree

    repo_root, index_db, local_store = _open_repo_storage()

    try:
        current = short_branch_name(index_db.get_current_branch())
        other = branch

        if other == current:
            _fail(f"already on '{current}'")

        other_head = index_db.get_ref(branch_ref_name(other))
        if not other_head:
            _fail(f"branch not found: {other}")

        current_head = index_db.get_branch_head(current)
        if not current_head:
            _fail(f"branch '{current}' has no commits")

        # Fast-forward when the current branch is an ancestor of the other,
        # i.e. the other branch is simply ahead and shares our history.
        if index_db.is_ancestor(current_head, other_head):
            index_db.set_branch_head(current, other_head)
            _print_success(f"Fast-forwarded {current} to {other_head[:12]}")
            _print_plain(
                f"Run 'blob checkout {current}' to update the working tree.",
                style="dim",
            )
            return

        # Collect each side's file -> ordered chunk hashes.
        def _snapshot(commit_hash: str) -> dict[str, list[str]]:
            grouped: dict[str, list[str]] = defaultdict(list)
            for ref in index_db.get_commit_chunk_refs(commit_hash):
                grouped[ref["file_path"]].append(ref["chunk_hash"])
            return {path: sorted(set(hashes), key=hashes.index) for path, hashes in grouped.items()}

        ours = _snapshot(current_head)
        theirs = _snapshot(other_head)

        merged: dict[str, list[str]] = {}
        conflicts: list[str] = []

        for path in sorted(set(ours) | set(theirs)):
            in_ours = path in ours
            in_theirs = path in theirs
            if in_ours and not in_theirs:
                merged[path] = ours[path]
            elif in_theirs and not in_ours:
                merged[path] = theirs[path]
            elif ours[path] == theirs[path]:
                merged[path] = ours[path]
            else:
                conflicts.append(path)

        if conflicts:
            _print_error(f"{len(conflicts)} file(s) changed on both branches to different content:")
            for path in conflicts[:20]:
                print(f"    conflict  {path}")
            if len(conflicts) > 20:
                print(f"    ... and {len(conflicts) - 20} more")
            _print_plain(
                "  Resolve by checking out one side, re-adding the file, and "
                "committing, then merge again.",
                style="dim",
            )
            sys.exit(1)

        # Rebuild offsets/lengths from stored chunk metadata.
        mappings: list[dict] = []
        combined: list[str] = []
        for path in sorted(merged):
            offset = 0
            for order, chunk_hash in enumerate(merged[path]):
                meta = index_db.get_chunk(chunk_hash) or {}
                length = meta.get("size_uncompressed", 0)
                mappings.append(
                    {
                        "file_path": path,
                        "chunk_hash": chunk_hash,
                        "chunk_offset": offset,
                        "chunk_length": length,
                        "chunk_order": order,
                        "size_uncompressed": length,
                        "size_compressed": meta.get("size_compressed", 0),
                    }
                )
                combined.append(chunk_hash)
                offset += length

        tree = build_tree(combined)
        if tree is None:
            _fail("nothing to merge")

        import time

        timestamp = time.time()
        message = f"Merge branch '{other}' into {current}"
        commit_hash = _compute_commit_hash(tree.hash, message, timestamp, current_head)

        index_db.save_commit(
            commit_hash=commit_hash,
            message=message,
            parent_hash=current_head,
            timestamp=timestamp,
            merkle_root_hash=tree.hash,
            tree_data=serialize_tree(tree),
            file_chunk_mappings=mappings,
            parents=[current_head, other_head],
        )
        index_db.set_branch_head(current, commit_hash)

        files = len({m["file_path"] for m in mappings})
        chunks = len(mappings)
    finally:
        try:
            index_db.close()
        except Exception:
            pass

    _print_success(
        f"Merged '{other}' into '{current}' -> {commit_hash[:12]} "
        f"({files} file(s), {chunks} chunks, root {tree.hash[:12]}...)"
    )
    _print_plain(
        f"Run 'blob checkout {current}' to update the working tree.",
        style="dim",
    )


# ---------------------------------------------------------------------------
# push / pull
# ---------------------------------------------------------------------------


def _sync_stats_table(title: str, stats: dict, elapsed: float) -> None:
    """Render a transfer summary. Never raises on odd input."""
    transferred = stats.get("transferred_chunks", 0)
    skipped = stats.get("skipped_chunks", 0)
    transferred_bytes = stats.get("transferred_bytes", 0)
    commits_synced = stats.get("commits_synced", 0)

    bytes_str = _format_bytes(transferred_bytes)
    if elapsed > 0 and transferred_bytes > 0:
        throughput = transferred_bytes / elapsed
        if throughput >= 1024 * 1024:
            tp_str = f"{throughput / (1024 * 1024):.1f} MB/s"
        elif throughput >= 1024:
            tp_str = f"{throughput / 1024:.1f} KB/s"
        else:
            tp_str = f"{throughput:.0f} B/s"
    else:
        tp_str = "-"

    mode = "Merkle delta" if stats.get("used_merkle_delta") else "full sync"

    if HAS_RICH and Table is not None:
        from rich.text import Text

        table = Table(title=title, show_lines=False)
        table.add_column("Metric", style="cyan", no_wrap=True)
        table.add_column("Value", style="white")
        table.add_row("Commits synced", str(commits_synced))
        table.add_row("Chunks transferred", str(transferred))
        table.add_row("Chunks skipped (dedup)", str(skipped))
        table.add_row("Bytes transferred", bytes_str)
        table.add_row("Throughput", tp_str)
        table.add_row("Elapsed", f"{elapsed:.2f}s")
        table.add_row("Delta mode", Text(mode))
        console.print(table)
    else:
        print(f"{title}")
        print(f"  Commits synced:         {commits_synced}")
        print(f"  Chunks transferred:     {transferred}")
        print(f"  Chunks skipped (dedup): {skipped}")
        print(f"  Bytes transferred:      {bytes_str}")
        print(f"  Throughput:             {tp_str}")
        print(f"  Elapsed:                {elapsed:.2f}s")
        print(f"  Delta mode:             {mode}")


def cmd_push(remote: str) -> None:
    """Push commits and the chunks they need to a remote."""
    import time

    repo_root = resolve_repo_root()
    if repo_root is None:
        _fail("not a blobtrack repository. Run 'blob init' first.")

    try:
        remote_path = resolve_input_path(remote)
    except Exception as exc:
        _fail(f"invalid remote path: {exc}")

    if not remote_path.exists() and not remote_path.parent.exists():
        _fail(
            f"remote path not accessible: {remote}\n"
            "  Hint: provide a filesystem path, e.g. 'blob push D:\\backup\\repo'"
        )

    repo_root, index_db, local_store = _open_repo_storage()

    try:
        from blobtrack.storage.remote_sync import RemoteSync

        if not index_db.list_commits(limit=1, include_tree=False):
            _print_success("Nothing to push - no commits in this repository.")
            return

        started = time.time()
        stats = RemoteSync.push(remote_path=remote_path, local_store=local_store, local_db=index_db)
        elapsed = time.time() - started
    except FileNotFoundError as exc:
        _fail(f"remote path error: {exc}")
    except Exception as exc:
        _fail(f"push failed: {exc}")
    finally:
        try:
            index_db.close()
        except Exception:
            pass

    if stats.get("transferred_chunks", 0) == 0 and stats.get("commits_synced", 0) == 0:
        _print_success(f"Everything up-to-date ({remote_path}).")
        return

    print()
    _sync_stats_table("Push complete", stats, elapsed)
    _print_success(f"Pushed to {remote_path}")


def cmd_pull(remote: str) -> None:
    """Pull commits and their chunks from a remote.

    Never touches the working tree; run ``blob checkout <hash>`` afterwards.
    """
    import time

    repo_root = resolve_repo_root()
    if repo_root is None:
        _fail("not a blobtrack repository. Run 'blob init' first.")

    try:
        remote_path = resolve_input_path(remote)
    except Exception as exc:
        _fail(f"invalid remote path: {exc}")

    remote_bt = remote_path if remote_path.name == ".blobtrack" else remote_path / ".blobtrack"
    if not (remote_bt / "objects").is_dir():
        _fail(
            f"remote repository not found at: {remote}\n"
            f"  Expected .blobtrack/objects at {remote_bt / 'objects'}\n"
            "  Hint: provide the path to a blobtrack repository"
        )

    repo_root, index_db, local_store = _open_repo_storage()

    try:
        from blobtrack.storage.remote_sync import RemoteSync

        started = time.time()
        stats = RemoteSync.pull(remote_path=remote_path, local_store=local_store, local_db=index_db)
        elapsed = time.time() - started
    except FileNotFoundError as exc:
        _fail(f"remote repository error: {exc}")
    except Exception as exc:
        _fail(f"pull failed: {exc}")
    finally:
        try:
            index_db.close()
        except Exception:
            pass

    if stats.get("transferred_chunks", 0) == 0 and stats.get("commits_synced", 0) == 0:
        _print_success(f"Already up-to-date ({remote_path}).")
        return

    print()
    _sync_stats_table("Pull complete", stats, elapsed)
    _print_success(f"Pulled from {remote_path}")
    if stats.get("commits_synced", 0) > 0:
        _print_plain(
            "Use 'blob log' to view history, 'blob checkout <hash>' to restore a version.",
            style="dim",
        )
