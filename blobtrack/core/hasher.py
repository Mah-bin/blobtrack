import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple

STREAM_BUFFER_SIZE = 64 * 1024 * 1024


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_file_streaming(filepath: str) -> str:
    sha256 = hashlib.sha256()

    with open(filepath, "rb") as f:
        while True:
            buffer = f.read(STREAM_BUFFER_SIZE)
            if not buffer:
                break
            sha256.update(buffer)

    return sha256.hexdigest()


def hash_chunks_parallel(chunks_data: List[Tuple[int, bytes]], max_workers: int = 8) -> List[Tuple[int, str]]:
    results: List[Tuple[int, str]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(hash_bytes, data): index
            for index, data in chunks_data
        }

        for future in as_completed(future_to_index):
            index = future_to_index[future]
            chunk_hash = future.result()
            results.append((index, chunk_hash))

    results.sort(key=lambda x: x[0])
    return results
