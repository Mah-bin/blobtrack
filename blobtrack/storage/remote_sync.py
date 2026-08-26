"""
Remote delta synchronization module for blobtrack.
Transfers only missing/delta chunks and synchronizes commit metadata with remote repositories.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

from .index_db import IndexDB
from .local_store import LocalStore


class RemoteSync:
    """
    Handles delta push and pull operations between the local repository
    and a remote repository target (directory/filesystem based).
    """

    @staticmethod
    def _resolve_remote_paths(remote_path: Union[str, Path]) -> Tuple[Path, Path, Path]:
        """
        Resolve remote root, objects directory, and index database path.
        Supports both direct repo paths and .blobtrack directory paths.
        """
        root = Path(remote_path)
        if root.name == ".blobtrack":
            bt_dir = root
        else:
            bt_dir = root / ".blobtrack"
        
        objects_dir = bt_dir / "objects"
        db_path = bt_dir / "index.db"
        return bt_dir, objects_dir, db_path

    @classmethod
    def init_remote(cls, remote_path: Union[str, Path]) -> Tuple[LocalStore, IndexDB]:
        """Initialize remote .blobtrack repository layout and database."""
        bt_dir, objects_dir, db_path = cls._resolve_remote_paths(remote_path)
        bt_dir.mkdir(parents=True, exist_ok=True)
        remote_store = LocalStore(objects_dir)
        remote_db = IndexDB(db_path)
        return remote_store, remote_db

    @classmethod
    def push(
        cls,
        remote_path: Union[str, Path],
        local_store: LocalStore,
        local_db: Optional[IndexDB] = None,
        delta_chunks: Optional[Iterable[str]] = None,
        commit_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Push missing/delta chunks and commit records to the remote target.

        :param remote_path: Path to remote repository.
        :param local_store: Local chunk store instance.
        :param local_db: Local IndexDB instance for commit history sync.
        :param delta_chunks: Optional explicit list of chunk hashes to transfer.
        :param commit_hash: Optional specific commit hash to push.
        :return: Transfer summary stats.
        """
        remote_store, remote_db = cls.init_remote(remote_path)

        transferred_chunks = 0
        transferred_bytes = 0
        skipped_chunks = 0
        commits_synced = 0

        # 1. Determine candidate chunks to transfer
        if delta_chunks is not None:
            candidate_hashes = list(delta_chunks)
        elif commit_hash and local_db:
            refs = local_db.get_commit_chunk_refs(commit_hash)
            candidate_hashes = [ref["chunk_hash"] for ref in refs]
        else:
            # Sync all stored chunks
            candidate_hashes = local_store.list_chunks()

        # 2. Transfer only missing chunks (Delta Sync)
        for chunk_hash in candidate_hashes:
            if remote_store.has_chunk(chunk_hash):
                skipped_chunks += 1
                continue

            chunk_data = local_store.retrieve_chunk(chunk_hash)
            remote_store.store_chunk(chunk_hash, chunk_data)
            transferred_chunks += 1
            transferred_bytes += len(chunk_data)

            # Also ensure remote database records chunk metadata if local_db available
            if local_db and remote_db:
                chunk_meta = local_db.get_chunk(chunk_hash)
                if chunk_meta:
                    remote_db.record_chunk(
                        chunk_hash=chunk_hash,
                        size_uncompressed=chunk_meta.get("size_uncompressed", 0),
                        size_compressed=chunk_meta.get("size_compressed", len(chunk_data)),
                    )

        # 3. Synchronize commits and references
        if local_db and remote_db:
            commits_to_sync = []
            if commit_hash:
                c = local_db.get_commit(commit_hash)
                if c:
                    commits_to_sync.append(c)
            else:
                commits_to_sync = local_db.list_commits()

            # Sync in chronological order (oldest to newest)
            for c in reversed(commits_to_sync):
                c_hash = c["commit_hash"]
                if remote_db.get_commit(c_hash) is None:
                    # Get associated file chunk mappings
                    refs = local_db.get_commit_chunk_refs(c_hash)
                    file_chunk_mappings = [
                        {
                            "file_path": r["file_path"],
                            "chunk_hash": r["chunk_hash"],
                            "chunk_offset": r.get("chunk_offset", 0),
                            "chunk_length": r.get("chunk_length", 0),
                            "chunk_order": r.get("chunk_order", 0),
                        }
                        for r in refs
                    ]
                    remote_db.save_commit(
                        commit_hash=c_hash,
                        message=c["message"],
                        parent_hash=c.get("parent_hash"),
                        author=c.get("author"),
                        timestamp=c.get("timestamp"),
                        merkle_root_hash=c.get("merkle_root_hash"),
                        tree_data=c.get("tree_data"),
                        file_chunk_mappings=file_chunk_mappings,
                    )
                    commits_synced += 1

        remote_db.close()

        return {
            "transferred_chunks": transferred_chunks,
            "transferred_bytes": transferred_bytes,
            "skipped_chunks": skipped_chunks,
            "commits_synced": commits_synced,
        }

    @classmethod
    def pull(
        cls,
        remote_path: Union[str, Path],
        local_store: LocalStore,
        local_db: Optional[IndexDB] = None,
        commit_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Pull missing chunks and commit history from remote repository into local.

        :param remote_path: Path to remote repository.
        :param local_store: Local chunk store instance.
        :param local_db: Local IndexDB instance.
        :param commit_hash: Optional target commit hash to pull.
        :return: Transfer summary stats.
        """
        bt_dir, objects_dir, db_path = cls._resolve_remote_paths(remote_path)
        if not objects_dir.is_dir():
            raise FileNotFoundError(f"Remote repository objects not found at {objects_dir}")

        remote_store = LocalStore(objects_dir)
        remote_db = IndexDB(db_path) if db_path.is_file() else None

        transferred_chunks = 0
        transferred_bytes = 0
        skipped_chunks = 0
        commits_synced = 0

        # 1. Determine chunks to pull
        if commit_hash and remote_db:
            refs = remote_db.get_commit_chunk_refs(commit_hash)
            target_hashes = [r["chunk_hash"] for r in refs]
        else:
            target_hashes = remote_store.list_chunks()

        # 2. Download missing chunks into local store
        for chunk_hash in target_hashes:
            if local_store.has_chunk(chunk_hash):
                skipped_chunks += 1
                continue

            chunk_data = remote_store.retrieve_chunk(chunk_hash)
            local_store.store_chunk(chunk_hash, chunk_data)
            transferred_chunks += 1
            transferred_bytes += len(chunk_data)

            if local_db and remote_db:
                chunk_meta = remote_db.get_chunk(chunk_hash)
                if chunk_meta:
                    local_db.record_chunk(
                        chunk_hash=chunk_hash,
                        size_uncompressed=chunk_meta.get("size_uncompressed", 0),
                        size_compressed=chunk_meta.get("size_compressed", len(chunk_data)),
                    )

        # 3. Synchronize commits and references into local database
        if local_db and remote_db:
            commits_to_sync = []
            if commit_hash:
                c = remote_db.get_commit(commit_hash)
                if c:
                    commits_to_sync.append(c)
            else:
                commits_to_sync = remote_db.list_commits()

            for c in reversed(commits_to_sync):
                c_hash = c["commit_hash"]
                if local_db.get_commit(c_hash) is None:
                    refs = remote_db.get_commit_chunk_refs(c_hash)
                    file_chunk_mappings = [
                        {
                            "file_path": r["file_path"],
                            "chunk_hash": r["chunk_hash"],
                            "chunk_offset": r.get("chunk_offset", 0),
                            "chunk_length": r.get("chunk_length", 0),
                            "chunk_order": r.get("chunk_order", 0),
                        }
                        for r in refs
                    ]
                    local_db.save_commit(
                        commit_hash=c_hash,
                        message=c["message"],
                        parent_hash=c.get("parent_hash"),
                        author=c.get("author"),
                        timestamp=c.get("timestamp"),
                        merkle_root_hash=c.get("merkle_root_hash"),
                        tree_data=c.get("tree_data"),
                        file_chunk_mappings=file_chunk_mappings,
                    )
                    commits_synced += 1

        if remote_db:
            remote_db.close()

        return {
            "transferred_chunks": transferred_chunks,
            "transferred_bytes": transferred_bytes,
            "skipped_chunks": skipped_chunks,
            "commits_synced": commits_synced,
        }
