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
    """
    Phase 3: Build Merkle Tree from current tracked files, compute delta vs
    parent, and persist commit via Member 4 IndexDB.
    Increment 3: Merkle Tree + delta diffing + commit (Member 3+1)
    """
    if not message or not message.strip():
        _print_error("commit message cannot be empty. Use -m \"message\"")
        sys.exit(1)

    repo_root = _find_repo_root()
    if repo_root is None:
        _print_error("not a blobtrack repository. Run 'blobtrack init' first.")
        sys.exit(1)

    db_path = repo_root / ".blobtrack" / "index.db"
    objects_dir = repo_root / ".blobtrack" / "objects"

    try:
        from blobtrack.core.chunker import chunk_file_streaming
        from blobtrack.core.hasher import hash_bytes, process_chunks
        from blobtrack.core.merkle_tree import build_tree, deserialize_tree, serialize_tree
        from blobtrack.core.differ import compute_delta
        from blobtrack.storage.index_db import IndexDB
        from blobtrack.storage.local_store import LocalStore
    except ImportError as e:
        _print_error(f"missing dependency for commit: {e}")
        sys.exit(1)

    try:
        index_db = IndexDB(db_path)
        # Ensure LocalStore exists (for has_chunk checks if needed, though add already stored)
        local_store = LocalStore(objects_dir)
    except Exception as e:
        _print_error(f"failed to open repository storage: {e}")
        sys.exit(1)

    try:
        tracked = index_db.list_files(status="tracked")
        if not tracked:
            # Also check if any files listed at all (maybe no status filter)
            tracked = index_db.list_files()
        if not tracked:
            _print_error("no tracked files to commit. Run 'blobtrack add <file>' first.")
            sys.exit(1)

        # Sort by path for deterministic repo-level Merkle order (Contract 3 + 9)
        tracked_sorted = sorted(tracked, key=lambda f: f["path"])

        combined_hashes: list[str] = []
        file_chunk_mappings: list[dict] = []
        # For per-file verification and building file chunk order
        files_in_commit = 0
        total_chunks_in_commit = 0

        for file_rec in tracked_sorted:
            rel_posix = file_rec["path"]
            # Resolve file on disk: try repo_root / rel_posix, fallback to absolute if outside repo
            disk_path = repo_root / rel_posix
            if not disk_path.is_file():
                # Try absolute posix path (for files added outside repo)
                alt = pathlib.Path(rel_posix)
                if alt.is_file():
                    disk_path = alt
                else:
                    _print_error(f"tracked file not found on disk, skipping: {rel_posix}")
                    continue

            try:
                chunk_stream = chunk_file_streaming(str(disk_path))
                # Use process_chunks to get hash + lengths without reimplementing hasher
                for pchunk in process_chunks(chunk_stream, batch_size=16, max_workers=8):
                    combined_hashes.append(pchunk.hash)
                    file_chunk_mappings.append(
                        {
                            "file_path": rel_posix,
                            "chunk_hash": pchunk.hash,
                            "chunk_offset": pchunk.offset,
                            "chunk_length": pchunk.length,
                            "chunk_order": pchunk.index,
                            "size_uncompressed": pchunk.length,
                            "size_compressed": len(pchunk.compressed_data),
                        }
                    )
                    total_chunks_in_commit += 1
                files_in_commit += 1
            except (FileNotFoundError, ValueError) as e:
                _print_error(f"skipping {rel_posix}: {e}")
                continue
            except Exception as e:
                _print_error(f"failed to process {rel_posix}: {e}")
                sys.exit(1)

        if not combined_hashes:
            _print_error("no chunks to commit (all tracked files missing or empty)")
            sys.exit(1)

        # Build Merkle Tree repo-level (Contract 2: repo root = hash of ordered chunk hashes)
        new_tree = build_tree(combined_hashes)
        if new_tree is None:
            _print_error("failed to build Merkle tree")
            sys.exit(1)
        merkle_root = new_tree.hash
        tree_data = serialize_tree(new_tree)

        # Parent handling (Contract 6)
        latest = index_db.get_latest_commit()
        parent_hash = latest["commit_hash"] if latest else None
        parent_tree = None
        if latest and latest.get("tree_data"):
            try:
                import json as _json

                td = latest["tree_data"]
                td_str = _json.dumps(td) if isinstance(td, dict) else td
                parent_tree = deserialize_tree(td_str)
            except Exception:
                parent_tree = None

        # Commit hash (Contract 7): deterministic SHA-256 of merkle_root + message + timestamp + parent
        import time

        timestamp = time.time()
        # Use hash_bytes for commit hash - includes merkle_root, message, timestamp, parent for uniqueness
        commit_hash_input = f"{merkle_root}:{message}:{timestamp}:{parent_hash or ''}".encode("utf-8")
        commit_hash = hash_bytes(commit_hash_input)

        # Delta for logging (Contract 5): use positional compute_delta, also show by_set if needed
        delta_info = ""
        if parent_tree is not None:
            try:
                delta = compute_delta(parent_tree, new_tree)
                delta_info = f" | delta: +{len(delta['added'])} -{len(delta['removed'])} ={len(delta['unchanged'])}"
            except Exception:
                delta_info = ""

        # Persist via Member 4 IndexDB (Contract 8)
        try:
            index_db.save_commit(
                commit_hash=commit_hash,
                message=message,
                parent_hash=parent_hash,
                author=None,
                timestamp=timestamp,
                merkle_root_hash=merkle_root,
                tree_data=tree_data,
                file_chunk_mappings=file_chunk_mappings,
            )
        except Exception as e:
            _print_error(f"failed to save commit: {e}")
            sys.exit(1)

        _print_success(
            f"Committed {commit_hash[:12]} - {files_in_commit} file(s), {total_chunks_in_commit} chunks, root {merkle_root[:12]}...{delta_info} - \"{message}\""
        )
        if parent_hash:
            # Show parent link for history
            if HAS_RICH and console:
                console.print(f"[dim]parent {parent_hash[:12]} -> {commit_hash[:12]}[/dim]")
            else:
                print(f"parent {parent_hash[:12]} -> {commit_hash[:12]}")

    finally:
        try:
            index_db.close()
        except Exception:
            pass


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
