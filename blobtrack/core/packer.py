import zstandard as zstd

DEFAULT_COMPRESSION_LEVEL = 3


def compress(data: bytes, level: int = DEFAULT_COMPRESSION_LEVEL) -> bytes:
    compressor = zstd.ZstdCompressor(level=level)
    return compressor.compress(data)


def decompress(data: bytes) -> bytes:
    decompressor = zstd.ZstdDecompressor()
    return decompressor.decompress(data)
