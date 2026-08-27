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

    import time as _time
    _add_start = _time.time()

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

    # Progress bar for large files (rich only, degrades gracefully)
    use_progress = HAS_RICH and console and file_size > 10 * 1024 * 1024
    progress = None
    task_id = None
    if use_progress:
        try:
            from rich.progress import (
                BarColumn,
                Progress,
                SpinnerColumn,
                TextColumn,
                TaskProgressColumn,
                TimeElapsedColumn,
                TimeRemainingColumn,
            )
            progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TextColumn("•"),
                TextColumn("{task.fields[chunk_info]}"),
                TextColumn("•"),
                TimeElapsedColumn(),
                TextColumn("•"),
                TimeRemainingColumn(),
                console=console,
                transient=False,
            )
            progress.start()
            task_id = progress.add_task(
                f"[cyan]Adding {rel_posix}...", total=file_size, chunk_info="0 chunks"
            )
        except Exception:
            progress = None
            task_id = None

    try:
        for pchunk in processed_iter:
            total_chunks += 1
            total_uncompressed += pchunk.length
            total_compressed += len(pchunk.compressed_data)

            if progress is not None and task_id is not None:
                try:
                    progress.update(
                        task_id,
                        advance=pchunk.length,
                        chunk_info=f"{total_chunks} chunks ({new_chunks} new, {reused_chunks} reused)",
                    )
                except Exception:
                    pass

            # 5. Deduplicate via LocalStore
            try:
                if local_store.has_chunk(pchunk.hash):
                    reused_chunks += 1
                else:
                    local_store.store_chunk(pchunk.hash, pchunk.compressed_data)
                    new_chunks += 1
            except Exception as e:
                if progress:
                    try:
                        progress.stop()
                    except Exception:
                        pass
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
                if progress:
                    try:
                        progress.stop()
                    except Exception:
                        pass
                _print_error(f"database error recording chunk: {e}")
                sys.exit(1)

            # Update chunk_info after counts changed
            if progress is not None and task_id is not None:
                try:
                    progress.update(
                        task_id,
                        chunk_info=f"{total_chunks} chunks ({new_chunks} new, {reused_chunks} reused)",
                    )
                except Exception:
                    pass

    except SystemExit:
        if progress:
            try:
                progress.stop()
            except Exception:
                pass
        raise
    except ValueError as e:
        if progress:
            try:
                progress.stop()
            except Exception:
                pass
        _print_error(str(e))
        sys.exit(1)
    except Exception as e:
        if progress:
            try:
                progress.stop()
            except Exception:
                pass
        _print_error(f"failed to process chunks: {e}")
        sys.exit(1)
    finally:
        if progress:
            try:
                progress.stop()
            except Exception:
                pass

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

    # 8. Success output with elapsed time
    dedup_pct = (reused_chunks / total_chunks * 100) if total_chunks else 0
    _elapsed = _time.time() - _add_start
    # human-readable time: 0.8s, 12.3s, 1m23s
    if _elapsed < 60:
        _elapsed_str = f"{_elapsed:.1f}s"
    else:
        _m, _s = divmod(int(_elapsed), 60)
        _elapsed_str = f"{_m}m{_s:02d}s ({_elapsed:.1f}s)"
    _print_success(
        f"Added '{rel_posix}' -> {total_chunks} chunks ({new_chunks} new, {reused_chunks} reused, {dedup_pct:.1f}% dedup) "
        f"[{total_uncompressed} -> {total_compressed} bytes compressed] in {_elapsed_str}"
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

        # Progress for commit re-chunking (rich only, for large repos)
        total_commit_bytes = 0
        for fr in tracked_sorted:
            try:
                p = repo_root / fr["path"]
                if p.is_file():
                    total_commit_bytes += p.stat().st_size
                else:
                    alt = pathlib.Path(fr["path"])
                    if alt.is_file():
                        total_commit_bytes += alt.stat().st_size
            except Exception:
                pass

        use_commit_progress = HAS_RICH and console and total_commit_bytes > 10 * 1024 * 1024
        commit_progress = None
        commit_task = None
        if use_commit_progress:
            try:
                from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TaskProgressColumn, TimeElapsedColumn, TimeRemainingColumn
                commit_progress = Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TaskProgressColumn(),
                    TextColumn("•"),
                    TextColumn("{task.fields[info]}"),
                    TextColumn("•"),
                    TimeElapsedColumn(),
                    TextColumn("•"),
                    TimeRemainingColumn(),
                    console=console,
                    transient=False,
                )
                commit_progress.start()
                commit_task = commit_progress.add_task("[cyan]Committing...", total=total_commit_bytes, info=f"0/{len(tracked_sorted)} files")
            except Exception:
                commit_progress = None

        processed_bytes = 0
        for file_idx, file_rec in enumerate(tracked_sorted):
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
                    processed_bytes += pchunk.length
                    if commit_progress is not None and commit_task is not None:
                        try:
                            commit_progress.update(commit_task, completed=processed_bytes, info=f"{file_idx+1}/{len(tracked_sorted)} files • {total_chunks_in_commit} chunks")
                        except Exception:
                            pass
                files_in_commit += 1
                if commit_progress is not None and commit_task is not None:
                    try:
                        commit_progress.update(commit_task, info=f"{files_in_commit}/{len(tracked_sorted)} files • {total_chunks_in_commit} chunks")
                    except Exception:
                        pass
            except (FileNotFoundError, ValueError) as e:
                _print_error(f"skipping {rel_posix}: {e}")
                continue
            except Exception as e:
                if commit_progress:
                    try:
                        commit_progress.stop()
                    except Exception:
                        pass
                _print_error(f"failed to process {rel_posix}: {e}")
                sys.exit(1)

        if commit_progress:
            try:
                commit_progress.stop()
            except Exception:
                pass

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
    """
    Phase 4: Display commit historychronologically (newest first).
    Uses Member 4 IndexDB.list_commits()
    """
    repo_root = _find_repo_root()
    if repo_root is None:
        _print_error("not a blobtrack repository. Run 'blobtrack init' first.")
        sys.exit(1)

    db_path = repo_root / ".blobtrack" / "index.db"
    try:
        from blobtrack.storage.index_db import IndexDB
    except ImportError as e:
        _print_error(f"missing dependency for log: {e}")
        sys.exit(1)

    try:
        index_db = IndexDB(db_path)
        commits = index_db.list_commits()
    except Exception as e:
        _print_error(f"failed to read commit history: {e}")
        sys.exit(1)
    finally:
        try:
            index_db.close()
        except Exception:
            pass

    if not commits:
        _print_success("No commits yet. Use 'blobtrack commit -m \"message\"' to create one.")
        return

    # Rich table if available, else plain
    if HAS_RICH and console:
        from rich.table import Table

        table = Table(title=f"Commit history ({len(commits)} commits)", show_lines=True)
        table.add_column("Hash", style="cyan", no_wrap=True)
        table.add_column("Message", style="white")
        table.add_column("Author", style="green")
        table.add_column("Date", style="dim")
        table.add_column("Parent", style="yellow")
        for c in commits:
            import datetime

            ts = c.get("timestamp")
            try:
                dt = datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
            except Exception:
                dt = str(ts or "")
            table.add_row(
                c["commit_hash"][:12],
                c.get("message", "")[:50],
                c.get("author") or "-",
                dt,
                (c.get("parent_hash") or "")[:12] or "-",
            )
        console.print(table)
    else:
        for c in commits:
            print(f"commit {c['commit_hash']}")
            print(f"  Message: {c.get('message','')}")
            print(f"  Author: {c.get('author') or '-'}")
            print(f"  Date: {c.get('timestamp','')}")
            print(f"  Parent: {c.get('parent_hash') or '-'}")
            print(f"  Merkle: {c.get('merkle_root_hash','')[:12]}")
            print()

    # Also print short summary
    _print_success(f"Displayed {len(commits)} commit(s)")


def cmd_checkout(commit_hash: str) -> None:
    """
    Phase 4: Reconstruct files from a commit by retrieving, decompressing and
    concatenating chunks in chunk_order. Uses Member 4 IndexDB + LocalStore
    + Member 2 packer.decompress. Verifies via chunk_order, not row order.
    Policy A: restores tracked files, leaves untracked working-tree files alone.
    """
    if not commit_hash or not commit_hash.strip():
        _print_error("commit hash cannot be empty")
        sys.exit(1)
    # Allow short 12-char or full 64-char hex
    import re

    if not re.fullmatch(r"[0-9a-fA-F]{6,64}", commit_hash.strip()):
        _print_error(f"invalid commit hash format: {commit_hash}")
        sys.exit(1)

    repo_root = _find_repo_root()
    if repo_root is None:
        _print_error("not a blobtrack repository. Run 'blobtrack init' first.")
        sys.exit(1)

    # Resolve full hash if short provided (prefix search)
    db_path = repo_root / ".blobtrack" / "index.db"
    objects_dir = repo_root / ".blobtrack" / "objects"

    try:
        from blobtrack.core.packer import decompress
        from blobtrack.storage.index_db import IndexDB
        from blobtrack.storage.local_store import LocalStore
    except ImportError as e:
        _print_error(f"missing dependency for checkout: {e}")
        sys.exit(1)

    try:
        index_db = IndexDB(db_path)
        local_store = LocalStore(objects_dir)
    except Exception as e:
        _print_error(f"failed to open repository storage: {e}")
        sys.exit(1)

    try:
        # Resolve short hash to full if needed
        target_hash = commit_hash.strip()
        commit = index_db.get_commit(target_hash)
        if commit is None:
            # Try prefix search among all commits
            all_commits = index_db.list_commits()
            matches = [c for c in all_commits if c["commit_hash"].startswith(target_hash)]
            if len(matches) == 1:
                target_hash = matches[0]["commit_hash"]
                commit = matches[0]
            elif len(matches) > 1:
                _print_error(f"ambiguous commit hash prefix '{commit_hash}' matches {len(matches)} commits")
                sys.exit(1)
            else:
                _print_error(f"commit not found: {commit_hash}")
                sys.exit(1)

        refs = index_db.get_commit_chunk_refs(target_hash)
        if not refs:
            _print_error(f"commit {target_hash[:12]} has no file chunk references")
            sys.exit(1)

        # Group refs by file_path, sorted by chunk_order
        from collections import defaultdict

        grouped: dict[str, list[dict]] = defaultdict(list)
        for r in refs:
            grouped[r["file_path"]].append(r)
        for file_path in grouped:
            grouped[file_path] = sorted(grouped[file_path], key=lambda x: x["chunk_order"])

        restored_files = 0
        total_bytes = 0

        for file_path, chunk_refs in grouped.items():
            # Resolve output path: repo_root / file_path (repo-relative posix)
            # Use Policy A: restore tracked files, do not delete other working-tree files
            out_path = repo_root / pathlib.Path(file_path)
            # Ensure parent dirs exist
            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                _print_error(f"failed to create directory for {file_path}: {e}")
                sys.exit(1)

            # Progress for checkout (only for large restores)
            use_checkout_progress = HAS_RICH and console and len(chunk_refs) > 10
            co_progress = None
            co_task = None
            if use_checkout_progress:
                try:
                    from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TaskProgressColumn, TimeElapsedColumn, TimeRemainingColumn
                    co_progress = Progress(
                        SpinnerColumn(),
                        TextColumn("[progress.description]{task.description}"),
                        BarColumn(),
                        TaskProgressColumn(),
                        TimeElapsedColumn(),
                        TimeRemainingColumn(),
                        console=console,
                        transient=True,
                    )
                    co_progress.start()
                    co_task = co_progress.add_task(f"[cyan]Restoring {file_path}...", total=len(chunk_refs))
                except Exception:
                    co_progress = None

            # Reconstruct via ordered chunks: retrieve + decompress + concatenate
            # Write atomically via tmp + move, verify via chunk_order
            import tempfile

            tmp_fd, tmp_name = tempfile.mkstemp(dir=str(out_path.parent), prefix=".checkout_", suffix=".tmp")
            try:
                with open(tmp_fd, "wb") as out_f:
                    for idx, ref in enumerate(chunk_refs):
                        ch = ref["chunk_hash"]
                        try:
                            compressed = local_store.retrieve_chunk(ch)
                        except FileNotFoundError:
                            if co_progress:
                                try:
                                    co_progress.stop()
                                except Exception:
                                    pass
                            _print_error(f"required chunk {ch[:12]} missing for {file_path} (commit {target_hash[:12]})")
                            try:
                                out_f.close()
                            except Exception:
                                pass
                            try:
                                pathlib.Path(tmp_name).unlink(missing_ok=True)
                            except Exception:
                                pass
                            sys.exit(1)
                        try:
                            decompressed = decompress(compressed)
                        except Exception as e:
                            if co_progress:
                                try:
                                    co_progress.stop()
                                except Exception:
                                    pass
                            _print_error(f"failed to decompress chunk {ch[:12]}: {e}")
                            try:
                                pathlib.Path(tmp_name).unlink(missing_ok=True)
                            except Exception:
                                pass
                            sys.exit(1)

                        # Optional length check vs stored chunk_length
                        expected_len = ref.get("chunk_length")
                        if expected_len and len(decompressed) != expected_len:
                            if co_progress:
                                try:
                                    co_progress.stop()
                                except Exception:
                                    pass
                            _print_error(f"chunk length mismatch for {ch[:12]}: expected {expected_len}, got {len(decompressed)}")
                            pathlib.Path(tmp_name).unlink(missing_ok=True)
                            sys.exit(1)

                        out_f.write(decompressed)
                        total_bytes += len(decompressed)
                        if co_progress is not None and co_task is not None:
                            try:
                                co_progress.update(co_task, advance=1)
                            except Exception:
                                pass
                if co_progress:
                    try:
                        co_progress.stop()
                    except Exception:
                        pass

                # Verify reconstructed file hash if possible (optional integrity)
                # Compare file size vs sum of chunk_lengths
                expected_total = sum(r.get("chunk_length", 0) for r in chunk_refs)
                actual_total = pathlib.Path(tmp_name).stat().st_size
                if expected_total and actual_total != expected_total:
                    _print_error(f"reconstructed size mismatch for {file_path}: {actual_total} vs {expected_total}")

                # Atomic move
                import shutil

                # Backup existing file if exists (do not delete, just overwrite atomically)
                # Use replace for atomic
                pathlib.Path(tmp_name).replace(out_path)
                restored_files += 1
            except SystemExit:
                raise
            except Exception as e:
                try:
                    pathlib.Path(tmp_name).unlink(missing_ok=True)
                except Exception:
                    pass
                _print_error(f"failed to checkout {file_path}: {e}")
                sys.exit(1)

        _print_success(f"Checked out {target_hash[:12]} - restored {restored_files} file(s), {len(refs)} chunks, {total_bytes} bytes")
        _print_success(f"Commit: \"{commit.get('message','')}\" parent {commit.get('parent_hash','-') or '-'}")

    finally:
        try:
            index_db.close()
        except Exception:
            pass


def cmd_push(remote: str) -> None:
    """
    Phase 5: Push delta chunks and commit history to a remote repository.
    Uses Member 4 RemoteSync.push() — delta calculation, chunk deduplication,
    and commit synchronization are all handled inside RemoteSync.
    The CLI's job is: validate → delegate → display.
    """
    import time as _time

    # 1. Validate local repository
    repo_root = _find_repo_root()
    if repo_root is None:
        _print_error("not a blobtrack repository. Run 'blobtrack init' first.")
        sys.exit(1)

    # 2. Validate remote path
    remote_path = pathlib.Path(remote)
    if not remote_path.is_absolute():
        # Resolve relative to cwd
        remote_path = (pathlib.Path.cwd() / remote_path).resolve()
    else:
        remote_path = remote_path.resolve()

    # Check if "origin" or similar name was passed without being a real path
    # If it doesn't exist and isn't an absolute-looking path, it's likely
    # a named alias which we don't support
    if not remote_path.exists() and not remote_path.parent.exists():
        _print_error(
            f"remote path not accessible: {remote}\n"
            "  Hint: provide a filesystem path, e.g. 'blobtrack push D:\\backup\\repo'"
        )
        sys.exit(1)

    # 3. Open local storage handles
    db_path = repo_root / ".blobtrack" / "index.db"
    objects_dir = repo_root / ".blobtrack" / "objects"

    try:
        from blobtrack.storage.index_db import IndexDB
        from blobtrack.storage.local_store import LocalStore
        from blobtrack.storage.remote_sync import RemoteSync
    except ImportError as e:
        _print_error(f"missing dependency for push: {e}")
        sys.exit(1)

    try:
        index_db = IndexDB(db_path)
        local_store = LocalStore(objects_dir)
    except Exception as e:
        _print_error(f"failed to open repository storage: {e}")
        sys.exit(1)

    try:
        # 4. Check if there is anything to push
        commits = index_db.list_commits()
        if not commits:
            _print_success("Nothing to push — no commits in this repository.")
            return

        # 5. Delegate to RemoteSync.push()
        # RemoteSync handles: init_remote, delta detection, chunk transfer,
        # commit sync (oldest-to-newest), deduplication via has_chunk.
        start_time = _time.time()

        stats = RemoteSync.push(
            remote_path=remote_path,
            local_store=local_store,
            local_db=index_db,
        )

        elapsed = _time.time() - start_time

        # 6. Display results
        transferred = stats.get("transferred_chunks", 0)
        skipped = stats.get("skipped_chunks", 0)
        transferred_bytes = stats.get("transferred_bytes", 0)
        commits_synced = stats.get("commits_synced", 0)

        if transferred == 0 and commits_synced == 0:
            _print_success("Everything up-to-date.")
        else:
            # Format bytes nicely
            if transferred_bytes >= 1024 * 1024:
                bytes_str = f"{transferred_bytes / (1024 * 1024):.1f} MB"
            elif transferred_bytes >= 1024:
                bytes_str = f"{transferred_bytes / 1024:.1f} KB"
            else:
                bytes_str = f"{transferred_bytes} bytes"

            # Throughput
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

            if HAS_RICH and console:
                from rich.table import Table

                table = Table(title="Push complete", show_lines=False)
                table.add_column("Metric", style="cyan", no_wrap=True)
                table.add_column("Value", style="white")
                table.add_row("Commits synced", str(commits_synced))
                table.add_row("Chunks transferred", str(transferred))
                table.add_row("Chunks skipped (dedup)", str(skipped))
                table.add_row("Bytes transferred", bytes_str)
                table.add_row("Throughput", tp_str)
                table.add_row("Elapsed", f"{elapsed:.2f}s")
                console.print(table)
            else:
                print(f"Push complete")
                print(f"  Commits synced:        {commits_synced}")
                print(f"  Chunks transferred:    {transferred}")
                print(f"  Chunks skipped (dedup):{skipped}")
                print(f"  Bytes transferred:     {bytes_str}")
                print(f"  Throughput:            {tp_str}")
                print(f"  Elapsed:               {elapsed:.2f}s")

        _print_success(f"Pushed to {remote_path}")

    except FileNotFoundError as e:
        _print_error(f"remote path error: {e}")
        sys.exit(1)
    except Exception as e:
        _print_error(f"push failed: {e}")
        sys.exit(1)
    finally:
        try:
            index_db.close()
        except Exception:
            pass


def cmd_pull(remote: str) -> None:
    """
    Phase 5: Pull delta chunks and commit history from a remote repository.
    Uses Member 4 RemoteSync.pull() — chunk deduplication and commit
    synchronization are handled inside RemoteSync.
    Pull does NOT modify the working tree. Use 'blobtrack checkout <hash>'
    after pulling to restore a specific version.
    """
    import time as _time

    # 1. Validate local repository
    repo_root = _find_repo_root()
    if repo_root is None:
        _print_error("not a blobtrack repository. Run 'blobtrack init' first.")
        sys.exit(1)

    # 2. Validate remote path
    remote_path = pathlib.Path(remote)
    if not remote_path.is_absolute():
        remote_path = (pathlib.Path.cwd() / remote_path).resolve()
    else:
        remote_path = remote_path.resolve()

    # Verify remote exists and looks like a blobtrack repo
    # RemoteSync._resolve_remote_paths handles both dir and .blobtrack paths
    remote_bt = remote_path / ".blobtrack" if remote_path.name != ".blobtrack" else remote_path
    remote_objects = remote_bt / "objects"
    if not remote_objects.is_dir():
        _print_error(
            f"remote repository not found at: {remote}\n"
            f"  Expected .blobtrack/objects at {remote_objects}\n"
            "  Hint: provide the path to a blobtrack repository"
        )
        sys.exit(1)

    # 3. Open local storage handles
    db_path = repo_root / ".blobtrack" / "index.db"
    objects_dir = repo_root / ".blobtrack" / "objects"

    try:
        from blobtrack.storage.index_db import IndexDB
        from blobtrack.storage.local_store import LocalStore
        from blobtrack.storage.remote_sync import RemoteSync
    except ImportError as e:
        _print_error(f"missing dependency for pull: {e}")
        sys.exit(1)

    try:
        index_db = IndexDB(db_path)
        local_store = LocalStore(objects_dir)
    except Exception as e:
        _print_error(f"failed to open repository storage: {e}")
        sys.exit(1)

    try:
        # 4. Delegate to RemoteSync.pull()
        # RemoteSync handles: chunk transfer, deduplication,
        # commit sync (oldest-to-newest).
        start_time = _time.time()

        stats = RemoteSync.pull(
            remote_path=remote_path,
            local_store=local_store,
            local_db=index_db,
        )

        elapsed = _time.time() - start_time

        # 5. Display results
        transferred = stats.get("transferred_chunks", 0)
        skipped = stats.get("skipped_chunks", 0)
        transferred_bytes = stats.get("transferred_bytes", 0)
        commits_synced = stats.get("commits_synced", 0)

        if transferred == 0 and commits_synced == 0:
            _print_success("Already up-to-date.")
        else:
            # Format bytes nicely
            if transferred_bytes >= 1024 * 1024:
                bytes_str = f"{transferred_bytes / (1024 * 1024):.1f} MB"
            elif transferred_bytes >= 1024:
                bytes_str = f"{transferred_bytes / 1024:.1f} KB"
            else:
                bytes_str = f"{transferred_bytes} bytes"

            # Throughput
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

            if HAS_RICH and console:
                from rich.table import Table

                table = Table(title="Pull complete", show_lines=False)
                table.add_column("Metric", style="cyan", no_wrap=True)
                table.add_column("Value", style="white")
                table.add_row("Commits synced", str(commits_synced))
                table.add_row("Chunks transferred", str(transferred))
                table.add_row("Chunks skipped (dedup)", str(skipped))
                table.add_row("Bytes transferred", bytes_str)
                table.add_row("Throughput", tp_str)
                table.add_row("Elapsed", f"{elapsed:.2f}s")
                console.print(table)
            else:
                print(f"Pull complete")
                print(f"  Commits synced:        {commits_synced}")
                print(f"  Chunks transferred:    {transferred}")
                print(f"  Chunks skipped (dedup):{skipped}")
                print(f"  Bytes transferred:     {bytes_str}")
                print(f"  Throughput:            {tp_str}")
                print(f"  Elapsed:               {elapsed:.2f}s")

        _print_success(f"Pulled from {remote_path}")

        # Helpful hint for user
        if commits_synced > 0:
            if HAS_RICH and console:
                console.print(
                    f"[dim]Use 'blobtrack log' to view history, "
                    f"'blobtrack checkout <hash>' to restore a version.[/dim]"
                )
            else:
                print("Use 'blobtrack log' to view history, 'blobtrack checkout <hash>' to restore a version.")

    except FileNotFoundError as e:
        _print_error(f"remote repository error: {e}")
        sys.exit(1)
    except Exception as e:
        _print_error(f"pull failed: {e}")
        sys.exit(1)
    finally:
        try:
            index_db.close()
        except Exception:
            pass


def cmd_gc() -> None:
    """
    Phase 4: Garbage collect orphan chunks not referenced by any commit.
    Uses Member 4 IndexDB.get_active_chunk_hashes/get_orphan_chunks and
    LocalStore.garbage_collect. Only deletes orphans, preserves all active.
    """
    repo_root = _find_repo_root()
    if repo_root is None:
        _print_error("not a blobtrack repository. Run 'blobtrack init' first.")
        sys.exit(1)

    db_path = repo_root / ".blobtrack" / "index.db"
    objects_dir = repo_root / ".blobtrack" / "objects"

    try:
        from blobtrack.storage.index_db import IndexDB
        from blobtrack.storage.local_store import LocalStore
    except ImportError as e:
        _print_error(f"missing dependency for gc: {e}")
        sys.exit(1)

    try:
        index_db = IndexDB(db_path)
        local_store = LocalStore(objects_dir)
    except Exception as e:
        _print_error(f"failed to open repository storage: {e}")
        sys.exit(1)

    try:
        # Use IndexDB active set as source of truth (any commit retained)
        active_hashes = index_db.get_active_chunk_hashes()
        # Also get orphan list from DB for reporting
        orphan_list = index_db.get_orphan_chunks()
        stored_list = local_store.list_chunks()
        # Orphans are stored not in active OR DB orphans; use set logic for safety
        # LocalStore.garbage_collect does filesystem scan, IndexDB handles DB rows
        # First, filesystem GC
        deleted_fs, freed_bytes = local_store.garbage_collect(active_hashes)
        # Then DB GC: delete chunk records not in active
        # Recompute orphans after filesystem GC to avoid double count
        remaining_orphans = index_db.get_orphan_chunks()
        deleted_db = 0
        if remaining_orphans:
            deleted_db = index_db.delete_chunk_records(remaining_orphans)

        total_deleted = deleted_fs  # filesystem count is primary
        # Note: deleted_db may include same hashes, but DB delete is idempotent

        if total_deleted == 0 and deleted_db == 0:
            _print_success("Garbage collection: no orphan chunks found - all 0 orphans, 0 bytes freed")
        else:
            _print_success(
                f"Garbage collection: deleted {total_deleted} orphan chunk(s) from objects, "
                f"{deleted_db} DB record(s), freed {freed_bytes} bytes. Active chunks: {len(active_hashes)}, stored before: {len(stored_list)}"
            )

        # Verify all retained commits still checkoutable (integrity check via active set)
        # No deletion of active chunks should have happened - assert
        if HAS_RICH and console:
            console.print(f"[dim]Active: {len(active_hashes)}, Orphans removed: {total_deleted}[/dim]")

    except Exception as e:
        _print_error(f"garbage collection failed: {e}")
        sys.exit(1)
    finally:
        try:
            index_db.close()
        except Exception:
            pass
