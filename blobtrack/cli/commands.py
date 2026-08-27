"""blobtrack CLI command handlers - integration layer.

Member 1 - CLI & Integration Lead
Phase 1: cmd_init() fully implemented, all other commands are controlled placeholders
until Members 3/4 deliver their modules (Increments 2-5).
"""

import pathlib
import shutil
import sys

# Rich for beautiful output if available, fallback to plain print
try:
    from rich.console import Console

    console = Console()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    console = None


def _print_success(msg: str) -> None:
    if HAS_RICH and console:
        console.print(f"[green]{msg}[/green]")
    else:
        print(msg)


def _print_error(msg: str) -> None:
    if HAS_RICH and console:
        console.print(f"[red]Error: {msg}[/red]")
    else:
        print(f"Error: {msg}", file=sys.stderr)


def cmd_init(cwd: pathlib.Path | None = None) -> None:
    """
    Create a new blobtrack repository in the current directory.
    Creates .blobtrack/objects, .blobtrack/commits, .blobtrack/index.db
    Phase 1: creates empty index.db container only - schema owned by Member 4 later.
    """
    base = pathlib.Path.cwd() if cwd is None else pathlib.Path(cwd)
    base = base.resolve()
    blobtrack_dir = base / ".blobtrack"

    # Detect already initialized - must not destroy
    if blobtrack_dir.exists():
        _print_error(f"repository already initialized in {blobtrack_dir}")
        sys.exit(1)

    # Validate base is a directory
    if not base.is_dir():
        _print_error(f"not a directory: {base}")
        sys.exit(1)

    objects_dir = blobtrack_dir / "objects"
    commits_dir = blobtrack_dir / "commits"
    index_db = blobtrack_dir / "index.db"

    try:
        # Use 0o700 where supported (Windows will ignore but not fail)
        blobtrack_dir.mkdir(mode=0o700, exist_ok=False)
        try:
            blobtrack_dir.chmod(0o700)
        except Exception:
            pass

        objects_dir.mkdir(mode=0o700, exist_ok=False)
        try:
            objects_dir.chmod(0o700)
        except Exception:
            pass

        commits_dir.mkdir(mode=0o700, exist_ok=False)
        try:
            commits_dir.chmod(0o700)
        except Exception:
            pass

        # Phase 1: create empty index.db container only.
        # Member 4 will later provide storage/index_db.py:init_db() to create tables.
        # Try to delegate to Member 4 if available, otherwise touch empty file.
        try:
            from blobtrack.storage.index_db import init_db  # type: ignore

            init_db(index_db)
        except ImportError:
            # No Member 4 yet - create empty placeholder file
            index_db.touch(exist_ok=False)
        except Exception as e:
            # If init_db exists but fails, rollback and report
            raise e

    except FileExistsError:
        _print_error(f"repository already initialized in {blobtrack_dir}")
        sys.exit(1)
    except Exception as e:
        # Atomic rollback - don't leave half-created repo
        if blobtrack_dir.exists():
            try:
                shutil.rmtree(blobtrack_dir)
            except Exception:
                pass
        _print_error(f"failed to initialize repository: {e}")
        sys.exit(1)

    _print_success(f"Initialized empty blobtrack repository in {blobtrack_dir}")


def _find_repo_root(start: pathlib.Path | None = None) -> pathlib.Path | None:
    """Walk up from start (or cwd) to find .blobtrack directory. Returns repo root or None."""
    cur = (start or pathlib.Path.cwd()).resolve()
    for parent in [cur] + list(cur.parents):
        if (parent / ".blobtrack").is_dir():
            return parent
    return None


def cmd_add(filepath: str) -> None:
    """
    Phase 2: Chunk, compress, deduplicate and store a file.
    Uses Member 2: chunk_file_streaming + process_chunks (hash+compress)
           Member 4: LocalStore + IndexDB
    """
    # 1. Verify repository
    repo_root = _find_repo_root()
    if repo_root is None:
        _print_error("not a blobtrack repository (or any parent up to root). Run 'blobtrack init' first.")
        sys.exit(1)

    # 2. Validate filepath - resolve relative to cwd, block traversal escape if needed
    raw_path = pathlib.Path(filepath)
    # Resolve against cwd if relative
    if not raw_path.is_absolute():
        target = (pathlib.Path.cwd() / raw_path).resolve()
    else:
        target = raw_path.resolve()

    if not target.exists():
        _print_error(f"file not found: {filepath}")
        sys.exit(1)
    if not target.is_file():
        _print_error(f"not a file: {filepath}")
        sys.exit(1)

    # 3. Prepare storage handles
    try:
        from blobtrack.core.chunker import chunk_file_streaming
        from blobtrack.core.hasher import hash_file_streaming, process_chunks
        from blobtrack.storage.index_db import IndexDB
        from blobtrack.storage.local_store import LocalStore
    except ImportError as e:
        _print_error(f"missing dependency for add: {e}")
        sys.exit(1)

    objects_dir = repo_root / ".blobtrack" / "objects"
    db_path = repo_root / ".blobtrack" / "index.db"

    try:
        local_store = LocalStore(objects_dir)
        index_db = IndexDB(db_path)
    except Exception as e:
        _print_error(f"failed to open repository storage: {e}")
        sys.exit(1)

    # 4. Chunk + hash + compress via Member 2 streaming pipeline (never loads whole file)
    # Determine repo-relative path for DB registration
    try:
        rel_posix = str(target.relative_to(repo_root).as_posix())
    except ValueError:
        rel_posix = str(target.as_posix())

    try:
        file_size = target.stat().st_size
        last_modified = target.stat().st_mtime
    except OSError as e:
        _print_error(f"cannot stat file: {e}")
        sys.exit(1)

    # Handle empty file - chunker raises ValueError
    try:
        chunk_stream = chunk_file_streaming(str(target))
        # Quick check: peak first chunk to trigger empty-file error early
        # process_chunks handles hashing+compression in parallel batches
        processed_iter = process_chunks(chunk_stream, batch_size=16, max_workers=8)
    except (FileNotFoundError, ValueError) as e:
        _print_error(str(e))
        sys.exit(1)
    except Exception as e:
        _print_error(f"failed to chunk file: {e}")
        sys.exit(1)

    new_chunks = 0
    reused_chunks = 0
    total_chunks = 0
    total_uncompressed = 0
    total_compressed = 0

    try:
        for pchunk in processed_iter:
            total_chunks += 1
            total_uncompressed += pchunk.length
            total_compressed += len(pchunk.compressed_data)

            # 5. Deduplicate via LocalStore
            try:
                if local_store.has_chunk(pchunk.hash):
                    reused_chunks += 1
                else:
                    local_store.store_chunk(pchunk.hash, pchunk.compressed_data)
                    new_chunks += 1
            except Exception as e:
                _print_error(f"storage error for chunk {pchunk.hash[:12]}: {e}")
                sys.exit(1)

            # 6. Record chunk metadata in DB (idempotent)
            try:
                index_db.record_chunk(
                    chunk_hash=pchunk.hash,
                    size_uncompressed=pchunk.length,
                    size_compressed=len(pchunk.compressed_data),
                )
            except Exception as e:
                _print_error(f"database error recording chunk: {e}")
                sys.exit(1)

    except SystemExit:
        raise
    except ValueError as e:
        _print_error(str(e))
        sys.exit(1)
    except Exception as e:
        _print_error(f"failed to process chunks: {e}")
        sys.exit(1)

    if total_chunks == 0:
        _print_error(f"file is empty or produced no chunks: {filepath}")
        sys.exit(1)

    # 7. Register file metadata
    try:
        file_hash = hash_file_streaming(str(target))
        index_db.register_file(
            path=rel_posix,
            file_hash=file_hash,
            size=file_size,
            last_modified=last_modified,
            status="tracked",
        )
    except Exception as e:
        _print_error(f"failed to register file in database: {e}")
        sys.exit(1)
    finally:
        try:
            index_db.close()
        except Exception:
            pass

    # 8. Success output
    dedup_pct = (reused_chunks / total_chunks * 100) if total_chunks else 0
    _print_success(
        f"Added '{rel_posix}' -> {total_chunks} chunks ({new_chunks} new, {reused_chunks} reused, {dedup_pct:.1f}% dedup) "
        f"[{total_uncompressed} -> {total_compressed} bytes compressed]"
    )


def cmd_commit(message: str) -> None:
    _print_error("commit functionality is not available yet (Increment 3 - requires Merkle Tree & storage)")
    sys.exit(1)


def cmd_log() -> None:
    _print_error("log functionality is not available yet (Increment 4 - requires storage)")
    sys.exit(1)


def cmd_checkout(commit_hash: str) -> None:
    _print_error("checkout functionality is not available yet (Increment 4 - requires storage)")
    sys.exit(1)


def cmd_push(remote: str) -> None:
    _print_error("push functionality is not available yet (Increment 5 - requires remote sync)")
    sys.exit(1)


def cmd_pull(remote: str) -> None:
    _print_error("pull functionality is not available yet (Increment 5 - requires remote sync)")
    sys.exit(1)


def cmd_gc() -> None:
    _print_error("gc functionality is not available yet (Increment 4 - requires storage)")
    sys.exit(1)
