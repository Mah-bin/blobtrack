import os
from dataclasses import dataclass
from typing import List, Generator
from fastcdc import fastcdc

MIN_CHUNK_SIZE = 512 * 1024
AVG_CHUNK_SIZE = 2 * 1024 * 1024
MAX_CHUNK_SIZE = 8 * 1024 * 1024


@dataclass
class ChunkData:
    index: int
    offset: int
    length: int
    data: bytes

    def __repr__(self) -> str:
        return (
            f"ChunkData(index={self.index}, offset={self.offset}, "
            f"length={self.length})"
        )


def chunk_file_streaming(filepath: str) -> Generator[ChunkData, None, None]:
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

    with open(filepath, "rb") as f:
        for index, cdc_chunk in enumerate(cdc_chunks):
            f.seek(cdc_chunk.offset)
            raw_data = f.read(cdc_chunk.length)

            yield ChunkData(
                index=index,
                offset=cdc_chunk.offset,
                length=cdc_chunk.length,
                data=raw_data,
            )


def read_chunk_at(filepath: str, offset: int, length: int) -> bytes:
    with open(filepath, "rb") as f:
        f.seek(offset)
        return f.read(length)


def get_file_info(filepath: str) -> dict:
    file_size = os.path.getsize(filepath)
    return {
        "filename": os.path.basename(filepath),
        "filepath": os.path.abspath(filepath),
        "size_bytes": file_size,
        "size_human": _human_readable_size(file_size),
    }


def _human_readable_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"
