"""
core/merkle_tree.py
--------------------
Merkle Tree construction and (de)serialization for blobtrack.

A Merkle Tree lets us represent the entire fingerprint-state of a file as a
single root hash. If two files (or two versions of the same file) produce
the same root hash, they are guaranteed to be identical at the chunk level.
If the root hashes differ, walking down the tree lets us prune away any
subtree whose hash matches on both sides, so we only pay attention to the
parts of the file that actually changed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional


class MerkleNode:
    """A single node in the Merkle Tree.

    Leaf nodes correspond 1:1 with a chunk's SHA-256 fingerprint (as produced
    by hasher.hash_bytes). Internal nodes are the hash of their two children's
    hashes concatenated together.
    """

    __slots__ = ("hash", "left", "right", "is_leaf")

    def __init__(
        self,
        hash_val: str,
        left: Optional["MerkleNode"] = None,
        right: Optional["MerkleNode"] = None,
        is_leaf: bool = False,
    ):
        self.hash = hash_val
        self.left = left
        self.right = right
        self.is_leaf = is_leaf

    def __repr__(self) -> str:
        kind = "leaf" if self.is_leaf else "node"
        return f"<MerkleNode {kind} hash={self.hash[:10]}...>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MerkleNode):
            return NotImplemented
        return self.hash == other.hash and self.is_leaf == other.is_leaf


def _combine_hashes(left_hash: str, right_hash: str) -> str:
    """Hash two child hashes together to produce their parent's hash."""
    combined = (left_hash + right_hash).encode("utf-8")
    return hashlib.sha256(combined).hexdigest()


def build_tree(chunk_hashes: List[str]) -> Optional[MerkleNode]:
    """Build a Merkle Tree bottom-up from an ordered list of chunk fingerprints.

    Args:
        chunk_hashes: SHA-256 hex digests, one per chunk, in file order
                      (as produced by Member 2's chunker + hasher).

    Returns:
        The root MerkleNode, or None if chunk_hashes is empty.

    Notes:
        If a level has an odd number of nodes, the leftover node is carried
        up untouched to the next level instead of being paired with a copy
        of itself. (Duplicating the node object would make its chunk hash
        appear twice in collect_leaf_hashes(), silently corrupting the
        chunk list -- so we promote it instead, which keeps every chunk
        hash represented exactly once no matter how the tree balances.)
    """
    if not chunk_hashes:
        return None

    # Level 0: leaves, one per chunk, in file order.
    level: List[MerkleNode] = [MerkleNode(hash_val=h, is_leaf=True) for h in chunk_hashes]

    # A single-chunk file is its own root.
    if len(level) == 1:
        return level[0]

    # Repeatedly pair nodes until only the root remains.
    while len(level) > 1:
        next_level: List[MerkleNode] = []
        i = 0
        while i < len(level):
            if i + 1 < len(level):
                left, right = level[i], level[i + 1]
                parent_hash = _combine_hashes(left.hash, right.hash)
                next_level.append(MerkleNode(hash_val=parent_hash, left=left, right=right))
                i += 2
            else:
                # Odd one out: promote it unchanged rather than duplicating it.
                next_level.append(level[i])
                i += 1
        level = next_level

    return level[0]


def serialize_tree(root: Optional[MerkleNode]) -> str:
    """Serialize a Merkle Tree to a JSON string for storage on disk."""

    def _to_dict(node: Optional[MerkleNode]) -> Optional[Dict[str, Any]]:
        if node is None:
            return None
        return {
            "hash": node.hash,
            "is_leaf": node.is_leaf,
            "left": _to_dict(node.left),
            "right": _to_dict(node.right),
        }

    return json.dumps(_to_dict(root))


def deserialize_tree(data: str) -> Optional[MerkleNode]:
    """Reconstruct a Merkle Tree from a JSON string produced by serialize_tree()."""

    def _from_dict(d: Optional[Dict[str, Any]]) -> Optional[MerkleNode]:
        if d is None:
            return None
        return MerkleNode(
            hash_val=d["hash"],
            left=_from_dict(d.get("left")),
            right=_from_dict(d.get("right")),
            is_leaf=d["is_leaf"],
        )

    return _from_dict(json.loads(data))


def collect_leaf_hashes(root: Optional[MerkleNode]) -> List[str]:
    """Return every leaf (chunk) hash under this node, left-to-right in order."""
    if root is None:
        return []
    if root.is_leaf:
        return [root.hash]
    return collect_leaf_hashes(root.left) + collect_leaf_hashes(root.right)
