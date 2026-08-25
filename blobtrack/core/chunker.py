import os
from dataclasses import dataclass
from typing import List, Generator
from fastcdc import fastcdc

from blobtrack.core.hasher import hash_bytes
from blobtrack.core.packer import compress

MIN_CHUNK_SIZE = 512 * 1024
AVG_CHUNK_SIZE = 2 * 1024 * 1024
MAX_CHUNK_SIZE = 8 * 1024 * 1024


@dataclass
class Chunk:
    index: int
    offset: int
    length: int
    hash: str
    data: bytes

    def __repr__(self) -> str:
        return (
            f"Chunk(index={self.index}, offset={self.offset}, "
            f"length={self.length}, hash={self.hash[:12]}...)"
        )


def chunk_file(filepath: str) -> List[Chunk]:
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")

    file_size = os.path.getsize(filepath)
    if file_size == 0:
        raise ValueError(f"File is empty: {filepath}")

    chunks: List[Chunk] = []

    cdc_chunks = fastcdc(
        filepath,
        min_size=MIN_CHUNK_SIZE,
        avg_size=AVG_CHUNK_SIZE,
        max_size=MAX_CHUNK_SIZE,
    )

    for index, cdc_chunk in enumerate(cdc_chunks):
        raw_data = _read_chunk_data(filepath, cdc_chunk.offset, cdc_chunk.length)
        chunk_hash = hash_bytes(raw_data)

        chunk = Chunk(
            index=index,
            offset=cdc_chunk.offset,
            length=cdc_chunk.length,
            hash=chunk_hash,
            data=raw_data,
        )
        chunks.append(chunk)

    return chunks


def chunk_file_streaming(filepath: str) -> Generator[Chunk, None, None]:
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")

    file_size = os.path.getsize(filepath)
    if file_size == 0:
        raise ValueError(f"File is empty: {filepath}")

    cdc_chunks = fastcdc(
        filepath,
        min_size=MIN_CHUNK_SIZE,
        avg_size=AVG_CHUNK_SIZE,
        max_size=MAX_CHUNK_SIZE,
    )

    for index, cdc_chunk in enumerate(cdc_chunks):
        raw_data = _read_chunk_data(filepath, cdc_chunk.offset, cdc_chunk.length)
        chunk_hash = hash_bytes(raw_data)

        yield Chunk(
            index=index,
            offset=cdc_chunk.offset,
            length=cdc_chunk.length,
            hash=chunk_hash,
            data=raw_data,
        )


def get_file_info(filepath: str) -> dict:
    file_size = os.path.getsize(filepath)
    return {
        "filename": os.path.basename(filepath),
        "filepath": os.path.abspath(filepath),
        "size_bytes": file_size,
        "size_human": _human_readable_size(file_size),
    }


def _read_chunk_data(filepath: str, offset: int, length: int) -> bytes:
    with open(filepath, "rb") as f:
        f.seek(offset)
        return f.read(length)


def _human_readable_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"
