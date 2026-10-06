"""Repository path resolution and safety.

Every path that ends up in the database -- and every path that gets written
back out to disk during a checkout -- goes through here. Two classes of bug
this module exists to prevent:

1. **Escaping the repository.** ``checkout`` writes files. If the path it
   writes comes from a database row, and that row arrived from a remote via
   ``pull``, then a malicious or merely broken remote could ask us to write
   anywhere on the filesystem. Absolute paths and ``..`` segments must be
   rejected outright.

2. **Tracking the same file under two names.** ``add a.bin`` and
   ``add ./a.bin`` and ``add sub/../a.bin`` are one file. Normalizing to a
   single repo-relative POSIX string keeps the manifest sane.

The rule enforced throughout: a tracked path is always **relative to the
repository root** and always **inside** the repository root.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath


class UnsafePathError(ValueError):
    """Raised when a path resolves outside the repository root."""


def resolve_repo_root(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` looking for a ``.blobtrack`` directory.

    Returns the repository root, or None if no repository is found.
    """
    cur = Path(start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / ".blobtrack").is_dir():
            return candidate
    return None


def to_repo_relative(target: Path, repo_root: Path) -> str:
    """Convert an on-disk path to a repo-relative POSIX string.

    Args:
        target: The file to track.
        repo_root: Repository root.

    Returns:
        A relative POSIX path suitable for the ``files`` table, e.g.
        ``"assets/video.mp4"``.

    Raises:
        UnsafePathError: ``target`` is not inside ``repo_root``. Tracking a
            file outside the repository would make the snapshot
            unreconstructable from the repository alone, so it is refused
            rather than silently stored as an absolute path.
    """
    resolved = Path(target).resolve()
    root = Path(repo_root).resolve()

    try:
        relative = resolved.relative_to(root)
    except ValueError:
        raise UnsafePathError(
            f"{resolved} is outside the repository at {root}. "
            f"blobtrack only tracks files inside the repository root."
        ) from None

    return relative.as_posix()


def safe_join(repo_root: Path, stored_path: str) -> Path:
    """Turn a stored relative path back into an absolute path inside the repo.

    This is the write-side guard for ``checkout``, and it deliberately treats
    the stored value as untrusted input.

    Args:
        repo_root: Repository root.
        stored_path: Path as recorded in the database. May come from a remote.

    Returns:
        An absolute path guaranteed to be inside ``repo_root``.

    Raises:
        UnsafePathError: the path is absolute, escapes the root, or contains
            traversal segments.
    """
    raw = str(stored_path)

    # Reject Windows drive letters and UNC paths as well as POSIX absolutes.
    if os.path.isabs(raw) or raw.startswith(("/", "\\")) or ":" in raw.split("/")[0]:
        raise UnsafePathError(f"refusing to use absolute path from repository metadata: {raw!r}")

    pure = PurePosixPath(raw.replace("\\", "/"))
    if pure.is_absolute():
        raise UnsafePathError(f"refusing to use absolute path from repository metadata: {raw!r}")
    if any(part == ".." for part in pure.parts):
        raise UnsafePathError(f"refusing to use path that escapes the repository: {raw!r}")

    candidate = (Path(repo_root) / Path(*pure.parts)).resolve()
    root = Path(repo_root).resolve()

    # Belt and braces: even after the checks above, confirm containment.
    try:
        candidate.relative_to(root)
    except ValueError:
        raise UnsafePathError(
            f"refusing to use path that escapes the repository: {raw!r}"
        ) from None

    return candidate


def resolve_input_path(raw: str) -> Path:
    """Resolve a user-supplied command-line path to an absolute path.

    Relative paths are taken against the current working directory, which is
    what a user typing ``blob add ../foo.bin`` expects.
    """
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def is_within(path: Path, root: Path) -> bool:
    """True if ``path`` resolves to somewhere inside ``root``."""
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False
