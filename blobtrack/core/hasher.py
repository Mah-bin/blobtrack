import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Generator

from blobtrack.core.packer import compress

STREAM_BUFFER_SIZE = 64 * 1024 * 1024


@dataclass
class ProcessedChunk:
    index: int
    offset: int
    length: int
    hash: str
    compressed_data: bytes

    def __repr__(self) -> str:
        return (
            f"ProcessedChunk(index={self.index}, offset={self.offset}, "
            f"length={self.length}, hash={self.hash[:12]}...)"
        )


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


def _process_single_chunk(chunk_data) -> ProcessedChunk:
    chunk_hash = hash_bytes(chunk_data.data)
    compressed = compress(chunk_data.data)

    return ProcessedChunk(
        index=chunk_data.index,
        offset=chunk_data.offset,
        length=chunk_data.length,
        hash=chunk_hash,
        compressed_data=compressed,
    )


def process_chunks(
    chunk_stream: Generator,
    batch_size: int = 16,
    max_workers: int = 8,
) -> Generator[ProcessedChunk, None, None]:
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        batch = []

        for chunk in chunk_stream:
            batch.append(chunk)

            if len(batch) >= batch_size:
                futures = [executor.submit(_process_single_chunk, c) for c in batch]
                for future in futures:
                    yield future.result()
                batch = []

        if batch:
            futures = [executor.submit(_process_single_chunk, c) for c in batch]
            for future in futures:
                yield future.result()
