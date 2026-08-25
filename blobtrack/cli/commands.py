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


def cmd_add(filepath: str) -> None:
    _print_error("add functionality is not available yet (Increment 2 - requires chunking & storage)")
    sys.exit(1)


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
