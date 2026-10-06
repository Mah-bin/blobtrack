"""SQLite index database: metadata for files, commits, chunks and branches.

Schema
------
``files``          one row per tracked path (the staging area / manifest)
``commits``        immutable commit metadata, including the serialized Merkle tree
``chunks``         one row per chunk we have metadata for
``chunk_refs``     which chunks, in which order, make up which file in which commit
``refs``           branch names pointing at commits
``config``         small key/value settings (currently which branch is checked out)
``commit_parents`` full parent list, so merge commits with 2+ parents work

``parent_hash`` on ``commits`` is kept as the *first* parent for backwards
compatibility; ``commit_parents`` is authoritative when present.

All tables are created with ``IF NOT EXISTS``, so opening a repository
written by an older version transparently upgrades it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

DEFAULT_BRANCH = "main"
BRANCH_PREFIX = "refs/heads/"
HEAD_CONFIG_KEY = "HEAD"


def init_db(db_path: str | Path) -> IndexDB:
    """Create (or open) the metadata database. Returns the IndexDB handle."""
    return IndexDB(db_path)


def branch_ref_name(branch: str) -> str:
    """Map a short branch name to its full ref name."""
    return branch if branch.startswith(BRANCH_PREFIX) else f"{BRANCH_PREFIX}{branch}"


def short_branch_name(ref_name: str) -> str:
    """Inverse of :func:`branch_ref_name`."""
    return ref_name[len(BRANCH_PREFIX) :] if ref_name.startswith(BRANCH_PREFIX) else ref_name


class IndexDB:
    """Metadata database manager using SQLite in WAL mode."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self.init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Create or reuse the connection with the pragmas we depend on."""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA synchronous = NORMAL;")
            self._conn.execute("PRAGMA foreign_keys = ON;")
        return self._conn

    def init_db(self) -> None:
        """Create tables and indices if they are missing. Idempotent."""
        conn = self._get_connection()
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT UNIQUE NOT NULL,
                    file_hash TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    last_modified REAL,
                    status TEXT DEFAULT 'tracked',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS commits (
                    commit_hash TEXT PRIMARY KEY,
                    parent_hash TEXT,
                    message TEXT NOT NULL,
                    author TEXT,
                    timestamp REAL NOT NULL,
                    merkle_root_hash TEXT,
                    tree_data TEXT
                );

                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_hash TEXT PRIMARY KEY,
                    size_uncompressed INTEGER DEFAULT 0,
                    size_compressed INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS chunk_refs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    commit_hash TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    chunk_hash TEXT NOT NULL,
                    chunk_offset INTEGER DEFAULT 0,
                    chunk_length INTEGER DEFAULT 0,
                    chunk_order INTEGER DEFAULT 0,
                    FOREIGN KEY (commit_hash) REFERENCES commits (commit_hash) ON DELETE CASCADE,
                    FOREIGN KEY (chunk_hash) REFERENCES chunks (chunk_hash) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS refs (
                    ref_name TEXT PRIMARY KEY,
                    commit_hash TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS commit_parents (
                    commit_hash TEXT NOT NULL,
                    parent_hash TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    PRIMARY KEY (commit_hash, ordinal),
                    FOREIGN KEY (commit_hash) REFERENCES commits (commit_hash) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_chunk_refs_commit ON chunk_refs(commit_hash);
                CREATE INDEX IF NOT EXISTS idx_chunk_refs_chunk ON chunk_refs(chunk_hash);
                CREATE INDEX IF NOT EXISTS idx_chunk_refs_file ON chunk_refs(file_path);
                CREATE INDEX IF NOT EXISTS idx_commits_timestamp ON commits(timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_commit_parents_parent ON commit_parents(parent_hash);
                """
            )

    # -------------------------------------------------------------------------
    # Files Management
    # -------------------------------------------------------------------------

    def register_file(
        self,
        path: str,
        file_hash: str,
        size: int,
        last_modified: float | None = None,
        status: str = "tracked",
    ) -> None:
        """Register or update a tracked file entry."""
        norm_path = str(Path(path).as_posix())
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO files (path, file_hash, size, last_modified, status, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(path) DO UPDATE SET
                    file_hash = excluded.file_hash,
                    size = excluded.size,
                    last_modified = excluded.last_modified,
                    status = excluded.status,
                    updated_at = CURRENT_TIMESTAMP;
                """,
                (norm_path, file_hash, size, last_modified, status),
            )

    def get_file(self, path: str) -> dict[str, Any] | None:
        """Tracking record for a specific file path."""
        norm_path = str(Path(path).as_posix())
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT id, path, file_hash, size, last_modified, status, updated_at "
            "FROM files WHERE path = ?;",
            (norm_path,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def list_files(self, status: str | None = None) -> list[dict[str, Any]]:
        """All tracked files, optionally filtered by status, sorted by path."""
        conn = self._get_connection()
        if status:
            cursor = conn.execute(
                "SELECT id, path, file_hash, size, last_modified, status, updated_at "
                "FROM files WHERE status = ? ORDER BY path ASC;",
                (status,),
            )
        else:
            cursor = conn.execute(
                "SELECT id, path, file_hash, size, last_modified, status, updated_at "
                "FROM files ORDER BY path ASC;"
            )
        return [dict(row) for row in cursor.fetchall()]

    def remove_file(self, path: str) -> bool:
        """Stop tracking a file. Returns True if a row was removed.

        This only removes the path from the staging manifest. Chunks already
        referenced by existing commits are intentionally left alone; use
        ``blob gc`` to reclaim unreferenced ones.
        """
        norm_path = str(Path(path).as_posix())
        conn = self._get_connection()
        with conn:
            cursor = conn.execute("DELETE FROM files WHERE path = ?;", (norm_path,))
            return cursor.rowcount > 0

    # -------------------------------------------------------------------------
    # Chunks Management
    # -------------------------------------------------------------------------

    def record_chunk(
        self,
        chunk_hash: str,
        size_uncompressed: int = 0,
        size_compressed: int = 0,
    ) -> None:
        """Record chunk metadata (idempotent)."""
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO chunks (chunk_hash, size_uncompressed, size_compressed)
                VALUES (?, ?, ?);
                """,
                (chunk_hash, size_uncompressed, size_compressed),
            )

    def record_chunks(self, chunk_records: Iterable[dict[str, Any]]) -> None:
        """Batch record chunk entries."""
        records = [
            (
                r["chunk_hash"],
                r.get("size_uncompressed", 0),
                r.get("size_compressed", 0),
            )
            for r in chunk_records
        ]
        if not records:
            return
        conn = self._get_connection()
        with conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO chunks (chunk_hash, size_uncompressed, size_compressed)
                VALUES (?, ?, ?);
                """,
                records,
            )

    def get_chunk(self, chunk_hash: str) -> dict[str, Any] | None:
        """Metadata for a single chunk."""
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT chunk_hash, size_uncompressed, size_compressed, created_at "
            "FROM chunks WHERE chunk_hash = ?;",
            (chunk_hash,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def list_chunks(self) -> list[dict[str, Any]]:
        """Every recorded chunk."""
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT chunk_hash, size_uncompressed, size_compressed, created_at FROM chunks;"
        )
        return [dict(row) for row in cursor.fetchall()]

    def count_chunks(self) -> int:
        """How many chunk rows exist."""
        conn = self._get_connection()
        return int(conn.execute("SELECT COUNT(*) FROM chunks;").fetchone()[0])

    def count_commits(self) -> int:
        """How many commits exist."""
        conn = self._get_connection()
        return int(conn.execute("SELECT COUNT(*) FROM commits;").fetchone()[0])

    def get_latest_commit_commit_hash(self) -> str | None:
        """Hash of the most recent commit anywhere, or None.

        Only used to seed a branch pointer on repositories that predate the
        refs table. New commits always attach to the current branch.
        """
        conn = self._get_connection()
        row = conn.execute(
            "SELECT commit_hash FROM commits ORDER BY timestamp DESC LIMIT 1;"
        ).fetchone()
        return row[0] if row and row[0] else None

    # -------------------------------------------------------------------------
    # Commits & References Management
    # -------------------------------------------------------------------------

    def save_commit(
        self,
        commit_hash: str,
        message: str,
        parent_hash: str | None = None,
        author: str | None = None,
        timestamp: float | None = None,
        merkle_root_hash: str | None = None,
        tree_data: dict | str | None = None,
        file_chunk_mappings: list[dict[str, Any]] | None = None,
        parents: list[str] | None = None,
    ) -> str:
        """Atomically persist a commit and its chunk references.

        Args:
            commit_hash: Content-derived hash of this commit.
            message: Human-readable commit message.
            parent_hash: First parent. Kept for backwards compatibility; when
                ``parents`` is given it is ignored in favour of ``parents[0]``.
            parents: Full ordered parent list. Two entries produce a merge
                commit.
            file_chunk_mappings: One dict per chunk reference, carrying
                file_path, chunk_hash, chunk_offset, chunk_length, chunk_order
                and the compressed/uncompressed sizes.

        Returns:
            The commit hash.
        """
        if timestamp is None:
            timestamp = time.time()

        if parents:
            parent_list = [p for p in parents if p]
        elif parent_hash:
            parent_list = [parent_hash]
        else:
            parent_list = []
        primary_parent = parent_list[0] if parent_list else None

        serialized_tree = (
            json.dumps(tree_data) if isinstance(tree_data, (dict, list)) else tree_data
        )

        conn = self._get_connection()
        with conn:
            # 1. The commit row itself.
            conn.execute(
                """
                INSERT INTO commits (
                    commit_hash, parent_hash, message, author, timestamp,
                    merkle_root_hash, tree_data
                )
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    commit_hash,
                    primary_parent,
                    message,
                    author,
                    timestamp,
                    merkle_root_hash,
                    serialized_tree,
                ),
            )

            # 2. Chunk rows must exist before chunk_refs can reference them
            #    (chunk_refs has a foreign key onto chunks).
            chunk_entries: list[tuple] = []
            ref_entries: list[tuple] = []
            for idx, mapping in enumerate(file_chunk_mappings or []):
                chunk_hash = mapping["chunk_hash"]
                file_path = str(Path(mapping["file_path"]).as_posix())
                chunk_offset = mapping.get("chunk_offset", 0)
                chunk_length = mapping.get("chunk_length", 0)
                chunk_order = mapping.get("chunk_order", idx)
                size_uncompressed = mapping.get("size_uncompressed", chunk_length)
                size_compressed = mapping.get("size_compressed", 0)

                chunk_entries.append((chunk_hash, size_uncompressed, size_compressed))
                ref_entries.append(
                    (
                        commit_hash,
                        file_path,
                        chunk_hash,
                        chunk_offset,
                        chunk_length,
                        chunk_order,
                    )
                )

            if chunk_entries:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO chunks (chunk_hash, size_uncompressed, size_compressed)
                    VALUES (?, ?, ?);
                    """,
                    chunk_entries,
                )
                conn.executemany(
                    """
                    INSERT INTO chunk_refs (
                        commit_hash, file_path, chunk_hash,
                        chunk_offset, chunk_length, chunk_order
                    )
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    ref_entries,
                )

            # 3. Full parent list (merge support).
            conn.executemany(
                """
                INSERT OR IGNORE INTO commit_parents (commit_hash, parent_hash, ordinal)
                VALUES (?, ?, ?);
                """,
                [(commit_hash, parent, ordinal) for ordinal, parent in enumerate(parent_list)],
            )

        return commit_hash

    @staticmethod
    def _row_to_commit(row: sqlite3.Row) -> dict[str, Any]:
        res = dict(row)
        if res.get("tree_data"):
            try:
                res["tree_data"] = json.loads(res["tree_data"])
            except Exception:
                pass
        return res

    def get_commit(self, commit_hash: str) -> dict[str, Any] | None:
        """Commit metadata by hash."""
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT commit_hash, parent_hash, message, author, timestamp,
                   merkle_root_hash, tree_data
            FROM commits
            WHERE commit_hash = ?;
            """,
            (commit_hash,),
        )
        row = cursor.fetchone()
        return self._row_to_commit(row) if row else None

    def get_latest_commit(self) -> dict[str, Any] | None:
        """Most recently timestamped commit in the whole repository."""
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT commit_hash, parent_hash, message, author, timestamp,
                   merkle_root_hash, tree_data
            FROM commits
            ORDER BY timestamp DESC
            LIMIT 1;
            """
        )
        row = cursor.fetchone()
        return self._row_to_commit(row) if row else None

    def list_commits(
        self, limit: int | None = None, include_tree: bool = True
    ) -> list[dict[str, Any]]:
        """Commit history, newest first.

        Args:
            limit: Optional maximum number of rows.
            include_tree: When False the (potentially multi-megabyte)
                ``tree_data`` column is left out and ``tree_data`` is None.
                Anything that only needs history for display should pass
                False so it does not pay to parse every historical Merkle
                tree.
        """
        conn = self._get_connection()
        columns = (
            "commit_hash, parent_hash, message, author, timestamp, merkle_root_hash, tree_data"
            if include_tree
            else "commit_hash, parent_hash, message, author, timestamp, merkle_root_hash"
        )
        query = f"SELECT {columns} FROM commits ORDER BY timestamp DESC"
        if limit is not None and limit > 0:
            query += f" LIMIT {int(limit)}"

        cursor = conn.execute(query)
        commits = []
        for row in cursor.fetchall():
            item = dict(row)
            if include_tree and item.get("tree_data"):
                try:
                    item["tree_data"] = json.loads(item["tree_data"])
                except Exception:
                    pass
            commits.append(item)
        return commits

    def get_commit_chunk_refs(self, commit_hash: str) -> list[dict[str, Any]]:
        """Chunk references for a commit, ordered by file then chunk order."""
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT id, commit_hash, file_path, chunk_hash, chunk_offset,
                   chunk_length, chunk_order
            FROM chunk_refs
            WHERE commit_hash = ?
            ORDER BY file_path ASC, chunk_order ASC;
            """,
            (commit_hash,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_file_chunks_for_commit(self, commit_hash: str, file_path: str) -> list[dict[str, Any]]:
        """Ordered chunk list for one file inside one commit."""
        norm_path = str(Path(file_path).as_posix())
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT chunk_refs.id, chunk_refs.commit_hash, chunk_refs.file_path,
                   chunk_refs.chunk_hash, chunk_refs.chunk_offset,
                   chunk_refs.chunk_length, chunk_refs.chunk_order,
                   chunks.size_uncompressed, chunks.size_compressed
            FROM chunk_refs
            LEFT JOIN chunks ON chunk_refs.chunk_hash = chunks.chunk_hash
            WHERE chunk_refs.commit_hash = ? AND chunk_refs.file_path = ?
            ORDER BY chunk_refs.chunk_order ASC;
            """,
            (commit_hash, norm_path),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_commit_file_paths(self, commit_hash: str) -> list[str]:
        """Distinct file paths present in a commit, sorted."""
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT DISTINCT file_path FROM chunk_refs
            WHERE commit_hash = ?
            ORDER BY file_path ASC;
            """,
            (commit_hash,),
        )
        return [row[0] for row in cursor.fetchall()]

    def get_commit_chunk_hashes(self, commit_hash: str) -> list[str]:
        """Distinct chunk hashes referenced by a commit, in reference order."""
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT chunk_hash, MIN(id) AS first_id
            FROM chunk_refs
            WHERE commit_hash = ?
            GROUP BY chunk_hash
            ORDER BY first_id ASC;
            """,
            (commit_hash,),
        )
        return [row[0] for row in cursor.fetchall()]

    # -------------------------------------------------------------------------
    # Parents & Ancestry
    # -------------------------------------------------------------------------

    def get_commit_parents(self, commit_hash: str) -> list[str]:
        """Full ordered parent list for a commit.

        Falls back to the legacy ``commits.parent_hash`` column for commits
        created before ``commit_parents`` existed.
        """
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT parent_hash FROM commit_parents
            WHERE commit_hash = ?
            ORDER BY ordinal ASC;
            """,
            (commit_hash,),
        )
        parents = [row[0] for row in cursor.fetchall()]
        if parents:
            return parents

        row = conn.execute(
            "SELECT parent_hash FROM commits WHERE commit_hash = ?;", (commit_hash,)
        ).fetchone()
        if row and row[0]:
            return [row[0]]
        return []

    def is_ancestor(self, ancestor_hash: str, descendant_hash: str) -> bool:
        """True if ``ancestor_hash`` appears anywhere in ``descendant_hash``'s history."""
        if not ancestor_hash or not descendant_hash:
            return False
        if ancestor_hash == descendant_hash:
            return True

        seen = set()
        stack = [descendant_hash]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            for parent in self.get_commit_parents(current):
                if parent == ancestor_hash:
                    return True
                stack.append(parent)
        return False

    def get_history(self, commit_hash: str) -> list[str]:
        """Commit hashes reachable from ``commit_hash``, newest first."""
        if not commit_hash:
            return []
        history: list[str] = []
        seen = set()
        stack = [commit_hash]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            history.append(current)
            stack.extend(self.get_commit_parents(current))
        history.sort(key=lambda h: (self.get_commit(h) or {}).get("timestamp") or 0, reverse=True)
        return history

    def delete_commit(self, commit_hash: str) -> bool:
        """Delete a commit and cascade its chunk references and parents."""
        conn = self._get_connection()
        with conn:
            cursor = conn.execute("DELETE FROM commits WHERE commit_hash = ?;", (commit_hash,))
            return cursor.rowcount > 0

    # -------------------------------------------------------------------------
    # Refs (branches) & Config
    # -------------------------------------------------------------------------

    def set_ref(self, ref_name: str, commit_hash: str | None) -> None:
        """Point a ref at a commit (or clear it with None)."""
        conn = self._get_connection()
        with conn:
            if commit_hash is None:
                conn.execute("DELETE FROM refs WHERE ref_name = ?;", (ref_name,))
            else:
                conn.execute(
                    """
                    INSERT INTO refs (ref_name, commit_hash, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(ref_name) DO UPDATE SET
                        commit_hash = excluded.commit_hash,
                        updated_at = CURRENT_TIMESTAMP;
                    """,
                    (ref_name, commit_hash),
                )

    def get_ref(self, ref_name: str) -> str | None:
        """Commit a ref points at, or None."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT commit_hash FROM refs WHERE ref_name = ?;", (ref_name,)
        ).fetchone()
        return row[0] if row and row[0] else None

    def list_refs(self, prefix: str | None = BRANCH_PREFIX) -> list[dict[str, Any]]:
        """Refs, optionally filtered by prefix, sorted by name."""
        conn = self._get_connection()
        if prefix:
            cursor = conn.execute(
                "SELECT ref_name, commit_hash, updated_at FROM refs "
                "WHERE ref_name LIKE ? ORDER BY ref_name ASC;",
                (f"{prefix}%",),
            )
        else:
            cursor = conn.execute(
                "SELECT ref_name, commit_hash, updated_at FROM refs ORDER BY ref_name ASC;"
            )
        return [dict(row) for row in cursor.fetchall()]

    def delete_ref(self, ref_name: str) -> bool:
        """Remove a ref."""
        conn = self._get_connection()
        with conn:
            cursor = conn.execute("DELETE FROM refs WHERE ref_name = ?;", (ref_name,))
            return cursor.rowcount > 0

    def set_config(self, key: str, value: str) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO config (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                """,
                (key, value),
            )

    def get_config(self, key: str, default: str | None = None) -> str | None:
        conn = self._get_connection()
        row = conn.execute("SELECT value FROM config WHERE key = ?;", (key,)).fetchone()
        return row[0] if row and row[0] is not None else default

    def get_current_branch(self) -> str:
        """Short name of the branch commits land on. Defaults to ``main``."""
        return self.get_config(HEAD_CONFIG_KEY, branch_ref_name(DEFAULT_BRANCH)) or (
            branch_ref_name(DEFAULT_BRANCH)
        )

    def set_current_branch(self, branch: str) -> None:
        self.set_config(HEAD_CONFIG_KEY, branch_ref_name(branch))

    def get_branch_head(self, branch: str) -> str | None:
        return self.get_ref(branch_ref_name(branch))

    def set_branch_head(self, branch: str, commit_hash: str | None) -> None:
        self.set_ref(branch_ref_name(branch), commit_hash)

    def list_branches(self) -> list[dict[str, Any]]:
        """Branch refs as short-name dictionaries."""
        return [
            {"name": short_branch_name(r["ref_name"]), "commit_hash": r["commit_hash"]}
            for r in self.list_refs(BRANCH_PREFIX)
        ]

    # -------------------------------------------------------------------------
    # Garbage Collection & Reference Counting
    # -------------------------------------------------------------------------

    def get_active_chunk_hashes(self) -> set[str]:
        """Chunk hashes referenced by any commit (all reachable history)."""
        conn = self._get_connection()
        cursor = conn.execute("SELECT DISTINCT chunk_hash FROM chunk_refs;")
        return {row[0] for row in cursor.fetchall()}

    def get_orphan_chunks(self) -> list[str]:
        """Recorded chunks that no commit references."""
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT c.chunk_hash
            FROM chunks c
            LEFT JOIN chunk_refs r ON c.chunk_hash = r.chunk_hash
            WHERE r.chunk_hash IS NULL;
            """
        )
        return [row[0] for row in cursor.fetchall()]

    def delete_chunk_records(self, chunk_hashes: Iterable[str]) -> int:
        """Delete specific chunk rows. Idempotent."""
        hash_list = list(chunk_hashes)
        if not hash_list:
            return 0
        conn = self._get_connection()
        with conn:
            placeholders = ",".join("?" for _ in hash_list)
            cursor = conn.execute(
                f"DELETE FROM chunks WHERE chunk_hash IN ({placeholders});",
                hash_list,
            )
            return cursor.rowcount

    # -------------------------------------------------------------------------
    # Lifecycle & Cleanup
    # -------------------------------------------------------------------------

    def close(self) -> None:
        """Close the SQLite connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> IndexDB:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
