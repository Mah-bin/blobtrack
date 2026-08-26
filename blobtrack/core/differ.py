"""
core/differ.py
---------------
Delta diffing between two Merkle Trees.

Given the Merkle Tree of a file's previous commit and the tree of its current
state, these functions tell the rest of the system exactly which chunks are
new, which are gone, and which are unchanged -- so storage/remote_sync.py
only has to push/pull the handful of chunks that actually changed.
"""

from typing import Dict, List, Optional, Set

from .merkle_tree import MerkleNode, collect_leaf_hashes


def compute_delta(
    old_tree: Optional[MerkleNode], new_tree: Optional[MerkleNode]
) -> Dict[str, List[str]]:
    """Top-down structural diff between two Merkle Trees.

    Walks both trees in lockstep, position by position. Whenever the hash at
    the current position matches on both sides, the whole subtree is pruned
    (a matching hash proves every chunk underneath is identical) -- this is
    what makes diffing a 20GB file fast: we only ever look at the branches
    that actually changed.

    Returns:
        {"added": [...], "removed": [...], "unchanged": [...]}
        Each list holds chunk hashes (hex digests).

    Caveat:
        This positional walk assumes the two trees have roughly the same
        shape, which holds for most in-place edits (e.g. re-encoding a few
        seconds in the middle of a video). If chunks are inserted or removed
        in a way that shifts everything after them, a positional comparison
        can under-count how much content is actually shared, since a chunk
        that didn't change can still land in a different tree position.
        For that scenario use compute_delta_by_set() instead, which is exact
        because it compares chunk *content* rather than position.
    """
    added: List[str] = []
    removed: List[str] = []
    unchanged: List[str] = []

    def _walk(old_node: Optional[MerkleNode], new_node: Optional[MerkleNode]) -> None:
        if old_node is None and new_node is None:
            return

        if old_node is None:
            added.extend(collect_leaf_hashes(new_node))
            return

        if new_node is None:
            removed.extend(collect_leaf_hashes(old_node))
            return

        # Same hash at this position -> entire subtree is identical, prune.
        if old_node.hash == new_node.hash:
            unchanged.extend(collect_leaf_hashes(new_node))
            return

        # Hashes differ. If either side is a leaf, we've hit bottom: this
        # exact chunk slot was replaced by a different chunk.
        if old_node.is_leaf or new_node.is_leaf:
            removed.append(old_node.hash)
            added.append(new_node.hash)
            return

        # Both internal nodes, different hashes -> recurse to find exactly
        # what changed underneath.
        _walk(old_node.left, new_node.left)
        _walk(old_node.right, new_node.right)

    _walk(old_tree, new_tree)
    return {"added": added, "removed": removed, "unchanged": unchanged}


def compute_delta_by_set(
    old_tree: Optional[MerkleNode], new_tree: Optional[MerkleNode]
) -> Dict[str, List[str]]:
    """Content-based (position-independent) diff between two Merkle Trees.

    Collects the full set of leaf hashes on each side and compares them with
    set arithmetic instead of walking the trees in lockstep. This is the
    version that should actually drive push()/pull() in remote_sync.py:
    content-defined chunking means a chunk can shift position in the tree
    (e.g. something was inserted earlier in the file) without its own
    content changing at all. A positional walk would wrongly flag every
    chunk after the insertion point as "changed"; set comparison correctly
    recognizes them as already-stored content that never needs re-uploading.

    Returns:
        {"added": [...], "removed": [...], "unchanged": [...]}
    """
    old_hashes: Set[str] = set(collect_leaf_hashes(old_tree))
    new_hashes: Set[str] = set(collect_leaf_hashes(new_tree))

    return {
        "added": sorted(new_hashes - old_hashes),
        "removed": sorted(old_hashes - new_hashes),
        "unchanged": sorted(old_hashes & new_hashes),
    }
