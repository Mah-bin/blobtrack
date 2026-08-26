# BlobTrack — Storage Subsystem Documentation (Member 4)

This document provides the verified interfaces, database schemas, and integration contracts for the storage subsystem.

## 1. Storage Interfaces

### `blobtrack.storage.index_db`
- `init_db(db_path: Union[str, Path]) -> IndexDB`: Initializes the SQLite metadata database with WAL mode and creates required tables (`files`, `commits`, `chunks`, `chunk_refs`).
- `IndexDB(db_path: Union[str, Path])`:
  - `register_file(path, file_hash, size, last_modified, status='tracked') -> None`
  - `get_file(path) -> Optional[Dict]`
  - `list_files(status=None) -> List[Dict]`
  - `remove_file(path) -> bool`
  - `record_chunk(chunk_hash, size_uncompressed=0, size_compressed=0) -> None`
  - `record_chunks(chunk_records: Iterable[Dict]) -> None`
  - `save_commit(commit_hash, message, parent_hash=None, author=None, timestamp=None, merkle_root_hash=None, tree_data=None, file_chunk_mappings=None) -> str`
  - `get_commit(commit_hash) -> Optional[Dict]`
  - `list_commits(limit=None) -> List[Dict]`
  - `get_commit_chunk_refs(commit_hash) -> List[Dict]`
  - `get_file_chunks_for_commit(commit_hash, file_path) -> List[Dict]`
  - `get_active_chunk_hashes() -> Set[str]`
  - `get_orphan_chunks() -> List[str]`
  - `delete_chunk_records(chunk_hashes) -> int`

### `blobtrack.storage.local_store`
- `LocalStore(objects_dir: Union[str, Path])`:
  - `store_chunk(chunk_hash: str, data: bytes) -> bool` (Returns `True` if new, `False` if deduplicated)
  - `retrieve_chunk(chunk_hash: str) -> bytes` (Raises `FileNotFoundError` if missing)
  - `has_chunk(chunk_hash: str) -> bool` (Deduplication check)
  - `delete_chunk(chunk_hash: str) -> bool`
  - `list_chunks() -> List[str]`
  - `get_chunk_size(chunk_hash: str) -> int`
  - `garbage_collect(active_hashes: Set[str]) -> Tuple[int, int]` (Returns `(deleted_count, freed_bytes)`)

### `blobtrack.storage.remote_sync`
- `RemoteSync.push(remote_path, local_store, local_db=None, delta_chunks=None, commit_hash=None) -> Dict`
- `RemoteSync.pull(remote_path, local_store, local_db=None, commit_hash=None) -> Dict`

---

## 2. Test Verification

Run all storage tests:
```powershell
uv run pytest tests/test_index_db.py tests/test_local_store.py tests/test_remote_sync.py -v
```
