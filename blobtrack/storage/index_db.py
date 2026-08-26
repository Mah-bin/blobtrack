"""
SQLite index database implementation for blobtrack metadata.
Manages files, commits, chunks, and chunk references with WAL mode.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Union


def init_db(db_path: Union[str, Path]) -> IndexDB:
    """
    Initialize SQLite metadata database with schema and WAL mode enabled.
    Can be called directly by Member 1 (CLI init).
    """
    return IndexDB(db_path)


class IndexDB:
    """
    Metadata database manager using SQLite in WAL mode.
    Handles commit history, file manifests, and chunk reference tracking.
    """

    def __init__(self, db_path: Union[str, Path]):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self.init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Create or reuse connection with proper pragmas enabled."""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            self._conn.row_factory = sqlite3.Row
            # Enable WAL mode and foreign keys for high concurrency & integrity
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA synchronous = NORMAL;")
            self._conn.execute("PRAGMA foreign_keys = ON;")
        return self._conn

    def init_db(self) -> None:
        """Initialize database tables, indices, and pragmas."""
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

                CREATE INDEX IF NOT EXISTS idx_chunk_refs_commit ON chunk_refs(commit_hash);
                CREATE INDEX IF NOT EXISTS idx_chunk_refs_chunk ON chunk_refs(chunk_hash);
                CREATE INDEX IF NOT EXISTS idx_chunk_refs_file ON chunk_refs(file_path);
                CREATE INDEX IF NOT EXISTS idx_commits_timestamp ON commits(timestamp DESC);
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
        last_modified: Optional[float] = None,
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

    def get_file(self, path: str) -> Optional[Dict[str, Any]]:
        """Retrieve tracking record for a specific file path."""
        norm_path = str(Path(path).as_posix())
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT id, path, file_hash, size, last_modified, status, updated_at FROM files WHERE path = ?;",
            (norm_path,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def list_files(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all tracked files, optionally filtered by status."""
        conn = self._get_connection()
        if status:
            cursor = conn.execute(
                "SELECT id, path, file_hash, size, last_modified, status, updated_at FROM files WHERE status = ? ORDER BY path ASC;",
                (status,),
            )
        else:
            cursor = conn.execute(
                "SELECT id, path, file_hash, size, last_modified, status, updated_at FROM files ORDER BY path ASC;"
            )
        return [dict(row) for row in cursor.fetchall()]

    def remove_file(self, path: str) -> bool:
        """Remove a file from active tracking."""
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
        """Record chunk metadata in database (idempotent)."""
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO chunks (chunk_hash, size_uncompressed, size_compressed)
                VALUES (?, ?, ?);
                """,
                (chunk_hash, size_uncompressed, size_compressed),
            )

    def record_chunks(self, chunk_records: Iterable[Dict[str, Any]]) -> None:
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

    def get_chunk(self, chunk_hash: str) -> Optional[Dict[str, Any]]:
        """Retrieve metadata for a single chunk."""
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT chunk_hash, size_uncompressed, size_compressed, created_at FROM chunks WHERE chunk_hash = ?;",
            (chunk_hash,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def list_chunks(self) -> List[Dict[str, Any]]:
        """Retrieve all recorded chunks."""
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT chunk_hash, size_uncompressed, size_compressed, created_at FROM chunks;"
        )
        return [dict(row) for row in cursor.fetchall()]

    # -------------------------------------------------------------------------
    # Commits & References Management
    # -------------------------------------------------------------------------

    def save_commit(
        self,
        commit_hash: str,
        message: str,
        parent_hash: Optional[str] = None,
        author: Optional[str] = None,
        timestamp: Optional[float] = None,
        merkle_root_hash: Optional[str] = None,
        tree_data: Optional[Union[dict, str]] = None,
        file_chunk_mappings: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        Atomically save a commit and its file chunk references.
        """
        if timestamp is None:
            timestamp = time.time()
        
        serialized_tree = (
            json.dumps(tree_data) if isinstance(tree_data, (dict, list)) else tree_data
        )

        conn = self._get_connection()
        with conn:
            # 1. Insert Commit
            conn.execute(
                """
                INSERT INTO commits (commit_hash, parent_hash, message, author, timestamp, merkle_root_hash, tree_data)
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    commit_hash,
                    parent_hash,
                    message,
                    author,
                    timestamp,
                    merkle_root_hash,
                    serialized_tree,
                ),
            )

            # 2. Insert chunk records and chunk_refs
            if file_chunk_mappings:
                chunk_entries = []
                ref_entries = []
                for idx, mapping in enumerate(file_chunk_mappings):
                    chunk_hash = mapping["chunk_hash"]
                    file_path = str(Path(mapping["file_path"]).as_posix())
                    chunk_offset = mapping.get("chunk_offset", 0)
                    chunk_length = mapping.get("chunk_length", 0)
                    chunk_order = mapping.get("chunk_order", idx)
                    size_uncompressed = mapping.get("size_uncompressed", chunk_length)
                    size_compressed = mapping.get("size_compressed", 0)

                    chunk_entries.append((chunk_hash, size_uncompressed, size_compressed))
                    ref_entries.append((
                        commit_hash,
                        file_path,
                        chunk_hash,
                        chunk_offset,
                        chunk_length,
                        chunk_order,
                    ))

                # Ensure chunk records exist so foreign key constraint passes
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO chunks (chunk_hash, size_uncompressed, size_compressed)
                    VALUES (?, ?, ?);
                    """,
                    chunk_entries,
                )

                conn.executemany(
                    """
                    INSERT INTO chunk_refs (commit_hash, file_path, chunk_hash, chunk_offset, chunk_length, chunk_order)
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    ref_entries,
                )

        return commit_hash

    def get_commit(self, commit_hash: str) -> Optional[Dict[str, Any]]:
        """Retrieve commit metadata by commit hash."""
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT commit_hash, parent_hash, message, author, timestamp, merkle_root_hash, tree_data
            FROM commits
            WHERE commit_hash = ?;
            """,
            (commit_hash,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        if res.get("tree_data"):
            try:
                res["tree_data"] = json.loads(res["tree_data"])
            except Exception:
                pass
        return res

    def get_latest_commit(self) -> Optional[Dict[str, Any]]:
        """Retrieve the most recent commit in history."""
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT commit_hash, parent_hash, message, author, timestamp, merkle_root_hash, tree_data
            FROM commits
            ORDER BY timestamp DESC
            LIMIT 1;
            """
        )
        row = cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        if res.get("tree_data"):
            try:
                res["tree_data"] = json.loads(res["tree_data"])
            except Exception:
                pass
        return res

    def list_commits(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """List commit history in chronological order (newest first)."""
        conn = self._get_connection()
        query = """
            SELECT commit_hash, parent_hash, message, author, timestamp, merkle_root_hash, tree_data
            FROM commits
            ORDER BY timestamp DESC
        """
        if limit is not None and limit > 0:
            query += f" LIMIT {int(limit)}"

        cursor = conn.execute(query)
        commits = []
        for row in cursor.fetchall():
            item = dict(row)
            if item.get("tree_data"):
                try:
                    item["tree_data"] = json.loads(item["tree_data"])
                except Exception:
                    pass
            commits.append(item)
        return commits

    def get_commit_chunk_refs(self, commit_hash: str) -> List[Dict[str, Any]]:
        """Retrieve all chunk reference mappings for a given commit."""
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT id, commit_hash, file_path, chunk_hash, chunk_offset, chunk_length, chunk_order
            FROM chunk_refs
            WHERE commit_hash = ?
            ORDER BY file_path ASC, chunk_order ASC;
            """,
            (commit_hash,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_file_chunks_for_commit(self, commit_hash: str, file_path: str) -> List[Dict[str, Any]]:
        """Retrieve ordered chunk list for reconstructing a specific file in a commit."""
        norm_path = str(Path(file_path).as_posix())
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT chunk_refs.id, chunk_refs.commit_hash, chunk_refs.file_path, chunk_refs.chunk_hash,
                   chunk_refs.chunk_offset, chunk_refs.chunk_length, chunk_refs.chunk_order,
                   chunks.size_uncompressed, chunks.size_compressed
            FROM chunk_refs
            LEFT JOIN chunks ON chunk_refs.chunk_hash = chunks.chunk_hash
            WHERE chunk_refs.commit_hash = ? AND chunk_refs.file_path = ?
            ORDER BY chunk_refs.chunk_order ASC;
            """,
            (commit_hash, norm_path),
        )
        return [dict(row) for row in cursor.fetchall()]

    # -------------------------------------------------------------------------
    # Garbage Collection & Reference Counting
    # -------------------------------------------------------------------------

    def get_active_chunk_hashes(self) -> Set[str]:
        """Return the set of all chunk hashes referenced by any active commit."""
        conn = self._get_connection()
        cursor = conn.execute("SELECT DISTINCT chunk_hash FROM chunk_refs;")
        return {row[0] for row in cursor.fetchall()}

    def get_orphan_chunks(self) -> List[str]:
        """
        Find chunk records stored in the database that are not referenced
        by any commit in chunk_refs.
        """
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
        """Delete specific chunk records from the chunks table."""
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

    def delete_commit(self, commit_hash: str) -> bool:
        """Delete a commit and cascade-delete its chunk_refs."""
        conn = self._get_connection()
        with conn:
            cursor = conn.execute(
                "DELETE FROM commits WHERE commit_hash = ?;",
                (commit_hash,),
            )
            return cursor.rowcount > 0

    # -------------------------------------------------------------------------
    # Lifecycle & Cleanup
    # -------------------------------------------------------------------------

    def close(self) -> None:
        """Close SQLite connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> IndexDB:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
