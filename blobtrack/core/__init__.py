"""Core engine: chunking, hashing, compression, Merkle trees, integrity.

* :mod:`blobtrack.core.chunker`      — content-defined chunking (fastcdc)
* :mod:`blobtrack.core.hasher`       — SHA-256 + parallel compress pipeline
* :mod:`blobtrack.core.packer`       — zstd compression
* :mod:`blobtrack.core.merkle_tree`  — Merkle tree build/serialize
* :mod:`blobtrack.core.differ`       — content delta between two trees
* :mod:`blobtrack.core.integrity`    — chunk verification helpers
"""
