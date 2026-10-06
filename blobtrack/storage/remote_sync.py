"""Remote delta synchronization for blobtrack.

Design
------
Push and pull are both *delta* operations driven by commit history, not by
scanning the object store:

1. Compare the two sides' commit sets to find which commits the other side
   is missing (oldest first, so parents always arrive before children).
2. Ask the Merkle tree of the newest common commit for the content delta.
   With CDC this is both exact and cheap -- inserting a chunk earlier in a
   file shifts tree positions but not content, so a set-based diff sees the
   shared chunks correctly where a positional walk would not.
3. Transfer only the chunks in that delta, skipping anything the other side
   already has.

Transferring per-commit rather than per-store also means orphans never reach
the remote, and a repository that has been garbage-collected locally still
pushes cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from blobtrack.core.differ import compute_delta_by_set
from blobtrack.core.merkle_tree import deserialize_tree

from .index_db import IndexDB
from .local_store import LocalStore


def _tree_from_commit(commit: dict[str, Any] | None):
    """Rebuild a MerkleNode from a commit row, or None if unavailable.

    IndexDB hands back ``tree_data`` already json-decoded (a dict), while a row
    fetched straight from a connection would still be a JSON string. Accept
    both so callers do not have to care where the dict came from.
    """
    if not commit:
        return None

    tree_data = commit.get("tree_data")
    if not tree_data:
        return None

    if not isinstance(tree_data, str):
        try:
            tree_data = json.dumps(tree_data)
        except (TypeError, ValueError):
            return None

    try:
        return deserialize_tree(tree_data)
    except Exception:
        return None


class RemoteSync:
    """Delta push and pull between local and remote repositories.

    Both remotes are plain directories containing a ``.blobtrack`` folder.
    There is no network transport and no authentication; a "remote" is a
    filesystem path you trust.
    """

    @staticmethod
    def _resolve_remote_paths(remote_path: str | Path) -> tuple[Path, Path, Path]:
        """Resolve (blobtrack_dir, objects_dir, db_path) for a remote path.

        Accepts either a repository root or the ``.blobtrack`` directory
        itself so callers can pass whichever they have.
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
    def init_remote(cls, remote_path: str | Path) -> tuple[LocalStore, IndexDB]:
        """Create the remote layout and database if they do not exist yet."""
        bt_dir, objects_dir, db_path = cls._resolve_remote_paths(remote_path)
        bt_dir.mkdir(parents=True, exist_ok=True)
        remote_store = LocalStore(objects_dir)
        remote_db = IndexDB(db_path)
        return remote_store, remote_db

    # ------------------------------------------------------------------
    # Shared delta logic
    # ------------------------------------------------------------------

    @staticmethod
    def _missing_commits(source_db: IndexDB, target_db: IndexDB) -> list[dict[str, Any]]:
        """Commits present in source but absent from target, oldest first.

        Returns commit dicts (without tree_data, which is re-fetched per
        commit when needed to keep this scan cheap).
        """
        target_hashes = {c["commit_hash"] for c in target_db.list_commits(include_tree=False)}
        missing = [
            c
            for c in source_db.list_commits(include_tree=False)
            if c["commit_hash"] not in target_hashes
        ]
        missing.sort(key=lambda c: c["timestamp"])
        return missing

    @staticmethod
    def _newest_common_commit(source_db: IndexDB, target_db: IndexDB) -> dict[str, Any] | None:
        """The most recent commit both sides have, used as the diff baseline.

        Walks the source's own history newest-first and returns the first
        commit the target also has. That guarantees a shared ancestor exists,
        which is what makes a Merkle delta well defined.
        """
        target_hashes = {c["commit_hash"] for c in target_db.list_commits(include_tree=False)}
        if not target_hashes:
            return None

        for commit in source_db.list_commits(include_tree=False):
            if commit["commit_hash"] in target_hashes:
                return source_db.get_commit(commit["commit_hash"])
        return None

    @classmethod
    def _delta_chunk_hashes(
        cls,
        source_db: IndexDB,
        target_db: IndexDB,
        commits: list[dict[str, Any]],
    ) -> tuple[list[str], bool]:
        """Which chunk hashes must cross the wire for these commits.

        Returns:
            (chunk_hashes, used_merkle_delta)

        When both sides share a common ancestor we ask the Merkle tree for the
        content delta between that ancestor and the newest commit being
        transferred. Otherwise (first sync, or unrelated histories) we fall
        back to the union of the transferred commits' references, which is
        correct but less selective.

        Chunk order and multiplicity are preserved from the commit references:
        reconstruction only needs "which bytes, in what order", and the
        content-addressed store makes re-sending a duplicate pointless.
        """
        newest = commits[-1] if commits else None
        baseline = cls._newest_common_commit(source_db, target_db)

        if baseline is not None and newest is not None:
            old_tree = _tree_from_commit(baseline)
            new_tree = _tree_from_commit(source_db.get_commit(newest["commit_hash"]))
            if old_tree is not None and new_tree is not None:
                delta = compute_delta_by_set(old_tree, new_tree)
                return list(delta["added"]), True

        hashes: list[str] = []
        seen: set[str] = set()
        for commit in commits:
            for chunk_hash in source_db.get_commit_chunk_hashes(commit["commit_hash"]):
                if chunk_hash not in seen:
                    seen.add(chunk_hash)
                    hashes.append(chunk_hash)
        return hashes, False

    @staticmethod
    def _file_chunk_mappings(source_db: IndexDB, commit_hash: str) -> list[dict[str, Any]]:
        """Chunk references for a commit, in the shape save_commit expects."""
        return [
            {
                "file_path": r["file_path"],
                "chunk_hash": r["chunk_hash"],
                "chunk_offset": r.get("chunk_offset", 0),
                "chunk_length": r.get("chunk_length", 0),
                "chunk_order": r.get("chunk_order", 0),
            }
            for r in source_db.get_commit_chunk_refs(commit_hash)
        ]

    # ------------------------------------------------------------------
    # Push
    # ------------------------------------------------------------------

    @classmethod
    def push(
        cls,
        remote_path: str | Path,
        local_store: LocalStore,
        local_db: IndexDB | None = None,
        delta_chunks: list[str] | None = None,
        commit_hash: str | None = None,
    ) -> dict[str, Any]:
        """Push missing commits and their chunks to the remote.

        Args:
            remote_path: Remote repository path (created if absent).
            local_store: Local chunk store.
            local_db: Local index database. Without it only chunks transfer.
            delta_chunks: Explicit chunk list, bypassing delta computation.
            commit_hash: Push only this commit instead of every unsynced one.

        Returns:
            Stats dict with transferred_chunks, transferred_bytes,
            skipped_chunks and commits_synced.
        """
        remote_store, remote_db = cls.init_remote(remote_path)

        transferred_chunks = 0
        transferred_bytes = 0
        skipped_chunks = 0
        commits_synced = 0

        try:
            if local_db is not None:
                if commit_hash:
                    single = local_db.get_commit(commit_hash)
                    commits_to_sync = [single] if single else []
                else:
                    commits_to_sync = cls._missing_commits(local_db, remote_db)
            else:
                commits_to_sync = []

            # 1. Decide what needs to move.
            if delta_chunks is not None:
                candidate_hashes = list(delta_chunks)
                used_merkle = False
            elif commits_to_sync:
                candidate_hashes, used_merkle = cls._delta_chunk_hashes(
                    local_db, remote_db, commits_to_sync
                )
            elif local_store is not None:
                # No commit metadata available: fall back to a full store sync.
                candidate_hashes = local_store.list_chunks()
                used_merkle = False
            else:
                candidate_hashes = []
                used_merkle = False

            # 2. Move only what the remote is actually missing.
            for chunk_hash in candidate_hashes:
                if remote_store.has_chunk(chunk_hash):
                    skipped_chunks += 1
                    continue
                if not local_store.has_chunk(chunk_hash):
                    raise FileNotFoundError(
                        f"cannot push: chunk {chunk_hash[:12]} is referenced by a "
                        f"commit but missing from the local object store "
                        f"(run 'blob fsck')"
                    )

                chunk_data = local_store.retrieve_chunk(chunk_hash, verify=True)
                remote_store.store_chunk(chunk_hash, chunk_data)
                transferred_chunks += 1
                transferred_bytes += len(chunk_data)

                if local_db is not None:
                    meta = local_db.get_chunk(chunk_hash)
                    remote_db.record_chunk(
                        chunk_hash=chunk_hash,
                        size_uncompressed=(meta or {}).get("size_uncompressed", 0),
                        size_compressed=(meta or {}).get("size_compressed", len(chunk_data)),
                    )

            # 3. Sync commit metadata oldest-first so parents precede children.
            if commits_to_sync:
                for commit in commits_to_sync:
                    c_hash = commit["commit_hash"]
                    if remote_db.get_commit(c_hash) is not None:
                        continue
                    remote_db.save_commit(
                        commit_hash=c_hash,
                        message=commit["message"],
                        parent_hash=commit.get("parent_hash"),
                        author=commit.get("author"),
                        timestamp=commit.get("timestamp"),
                        merkle_root_hash=commit.get("merkle_root_hash"),
                        tree_data=commit.get("tree_data"),
                        file_chunk_mappings=cls._file_chunk_mappings(local_db, c_hash),
                        parents=local_db.get_commit_parents(c_hash),
                    )
                    commits_synced += 1
        finally:
            remote_db.close()

        return {
            "transferred_chunks": transferred_chunks,
            "transferred_bytes": transferred_bytes,
            "skipped_chunks": skipped_chunks,
            "commits_synced": commits_synced,
            "used_merkle_delta": used_merkle,
        }

    # ------------------------------------------------------------------
    # Pull
    # ------------------------------------------------------------------

    @classmethod
    def pull(
        cls,
        remote_path: str | Path,
        local_store: LocalStore,
        local_db: IndexDB | None = None,
        commit_hash: str | None = None,
    ) -> dict[str, Any]:
        """Pull missing commits and their chunks from the remote.

        Never touches the working tree -- run ``blob checkout <hash>`` after.

        Args:
            remote_path: Remote repository path. Must already exist.
            local_store: Local chunk store.
            local_db: Local index database.
            commit_hash: Pull only this commit instead of every unsynced one.

        Returns:
            Stats dict with transferred_chunks, transferred_bytes,
            skipped_chunks and commits_synced.
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
        used_merkle = False

        try:
            if commit_hash and remote_db is not None:
                single = remote_db.get_commit(commit_hash)
                commits_to_sync = [single] if single else []
            elif remote_db is not None and local_db is not None:
                commits_to_sync = cls._missing_commits(remote_db, local_db)
            else:
                commits_to_sync = []

            # 1. Decide what needs to move.
            if commit_hash and remote_db is not None:
                target_hashes = remote_db.get_commit_chunk_hashes(commit_hash)
            elif commits_to_sync:
                target_hashes, used_merkle = cls._delta_chunk_hashes(
                    remote_db, local_db, commits_to_sync
                )
            else:
                target_hashes = remote_store.list_chunks()

            # 2. Move only what we are actually missing.
            for chunk_hash in target_hashes:
                if local_store.has_chunk(chunk_hash):
                    skipped_chunks += 1
                    continue
                if not remote_store.has_chunk(chunk_hash):
                    raise FileNotFoundError(
                        f"cannot pull: chunk {chunk_hash[:12]} is referenced by a "
                        f"remote commit but missing from the remote object store"
                    )

                chunk_data = remote_store.retrieve_chunk(chunk_hash, verify=True)
                local_store.store_chunk(chunk_hash, chunk_data)
                transferred_chunks += 1
                transferred_bytes += len(chunk_data)

                if local_db is not None and remote_db is not None:
                    meta = remote_db.get_chunk(chunk_hash)
                    local_db.record_chunk(
                        chunk_hash=chunk_hash,
                        size_uncompressed=(meta or {}).get("size_uncompressed", 0),
                        size_compressed=(meta or {}).get("size_compressed", len(chunk_data)),
                    )

            # 3. Sync commit metadata oldest-first so parents precede children.
            if local_db is not None and commits_to_sync:
                for commit in commits_to_sync:
                    c_hash = commit["commit_hash"]
                    if local_db.get_commit(c_hash) is not None:
                        continue
                    local_db.save_commit(
                        commit_hash=c_hash,
                        message=commit["message"],
                        parent_hash=commit.get("parent_hash"),
                        author=commit.get("author"),
                        timestamp=commit.get("timestamp"),
                        merkle_root_hash=commit.get("merkle_root_hash"),
                        tree_data=commit.get("tree_data"),
                        file_chunk_mappings=cls._file_chunk_mappings(remote_db, c_hash),
                        parents=remote_db.get_commit_parents(c_hash),
                    )
                    commits_synced += 1
        finally:
            if remote_db is not None:
                remote_db.close()

        return {
            "transferred_chunks": transferred_chunks,
            "transferred_bytes": transferred_bytes,
            "skipped_chunks": skipped_chunks,
            "commits_synced": commits_synced,
            "used_merkle_delta": used_merkle,
        }
