"""Merkle delta correctness tests.

The central claim of this project is that a localized edit to a large file
produces a small transfer. These tests exercise that claim directly, including
the insert-at-the-front case that breaks a naive positional diff.
"""

import hashlib

from blobtrack.core.differ import compute_delta, compute_delta_by_set
from blobtrack.core.merkle_tree import (
    build_tree,
    collect_leaf_hashes,
    deserialize_tree,
    serialize_tree,
)


def _hashes(n: int, salt: str = "") -> list:
    return [hashlib.sha256(f"{salt}{i}".encode()).hexdigest() for i in range(n)]


# ---------------------------------------------------------------------------
# Tree construction
# ---------------------------------------------------------------------------


def test_empty_tree_is_none():
    assert build_tree([]) is None


def test_single_chunk_is_its_own_root():
    leaf = _hashes(1)[0]
    tree = build_tree([leaf])
    assert tree.hash == leaf
    assert tree.is_leaf is True


def test_leaf_roundtrip_preserves_order():
    hashes = _hashes(7)
    assert collect_leaf_hashes(build_tree(hashes)) == hashes


def test_leaf_recovery_holds_for_odd_sizes():
    for n in (1, 2, 3, 5, 8, 9, 17, 33):
        hashes = _hashes(n)
        assert collect_leaf_hashes(build_tree(hashes)) == hashes, f"failed at n={n}"


def test_serialization_roundtrip():
    hashes = _hashes(11)
    tree = build_tree(hashes)
    restored = deserialize_tree(serialize_tree(tree))
    assert restored.hash == tree.hash
    assert collect_leaf_hashes(restored) == hashes


def test_root_is_deterministic():
    hashes = _hashes(23)
    assert build_tree(hashes).hash == build_tree(hashes).hash


def test_root_changes_when_content_changes():
    assert build_tree(_hashes(10)).hash != build_tree(_hashes(10, salt="x")).hash


def test_order_matters():
    forward = build_tree(_hashes(6))
    reversed_order = build_tree(list(reversed(_hashes(6))))
    assert forward.hash != reversed_order.hash


# ---------------------------------------------------------------------------
# Positional diff (kept, but documented as approximate)
# ---------------------------------------------------------------------------


def test_positional_delta_finds_single_leaf_change():
    hashes = _hashes(9)
    changed = list(hashes)
    changed[4] = hashlib.sha256(b"changed").hexdigest()
    delta = compute_delta(build_tree(hashes), build_tree(changed))
    assert len(delta["added"]) == 1
    assert len(delta["removed"]) == 1


def test_positional_delta_is_zero_for_identical_trees():
    hashes = _hashes(12)
    delta = compute_delta(build_tree(hashes), build_tree(hashes))
    assert delta["added"] == [] and delta["removed"] == []


# ---------------------------------------------------------------------------
# Set-based diff: the one push/pull actually uses
# ---------------------------------------------------------------------------


def test_by_set_detects_one_leaf_change():
    hashes = _hashes(9)
    changed = list(hashes)
    changed[4] = hashlib.sha256(b"changed").hexdigest()
    delta = compute_delta_by_set(build_tree(hashes), build_tree(changed))
    assert len(delta["added"]) == 1
    assert len(delta["removed"]) == 1
    assert len(delta["unchanged"]) == 8


def test_by_set_is_correct_when_a_chunk_is_inserted_at_the_front():
    """A positional walk reports every chunk as changed here. The set-based
    diff must report exactly one added chunk."""
    original = _hashes(2001)
    inserted = hashlib.sha256(b"inserted at front").hexdigest()
    shifted = [inserted] + original

    delta = compute_delta_by_set(build_tree(original), build_tree(shifted))

    assert delta["added"] == [inserted]
    assert delta["removed"] == []
    assert len(delta["unchanged"]) == 2001


def test_by_set_handles_insert_in_the_middle():
    original = _hashes(500)
    inserted = hashlib.sha256(b"inserted mid").hexdigest()
    shifted = original[:250] + [inserted] + original[250:]

    delta = compute_delta_by_set(build_tree(original), build_tree(shifted))

    assert delta["added"] == [inserted]
    assert len(delta["unchanged"]) == 500


def test_by_set_handles_deletion():
    original = _hashes(300)
    shortened = original[:150]

    delta = compute_delta_by_set(build_tree(original), build_tree(shortened))

    assert len(delta["removed"]) == 150
    assert delta["added"] == []
    assert len(delta["unchanged"]) == 150


def test_by_set_handles_replacement():
    original = _hashes(100)
    replacement = hashlib.sha256(b"brand new").hexdigest()
    updated = original[:50] + [replacement] + original[51:]

    delta = compute_delta_by_set(build_tree(original), build_tree(updated))

    assert delta["added"] == [replacement]
    assert delta["removed"] == [original[50]]
    assert len(delta["unchanged"]) == 99


def test_by_set_output_is_sorted_and_deterministic():
    original = _hashes(50)
    updated = list(original)
    updated[10] = hashlib.sha256(b"z").hexdigest()
    updated[20] = hashlib.sha256(b"a").hexdigest()

    first = compute_delta_by_set(build_tree(original), build_tree(updated))
    second = compute_delta_by_set(build_tree(original), build_tree(updated))

    assert first == second
    assert first["added"] == sorted(first["added"])


def test_by_set_against_empty_tree_reports_everything_added():
    hashes = _hashes(64)
    delta = compute_delta_by_set(None, build_tree(hashes))
    assert len(delta["added"]) == 64
    assert delta["removed"] == []


def test_by_set_against_none_reports_everything_removed():
    hashes = _hashes(64)
    delta = compute_delta_by_set(build_tree(hashes), None)
    assert len(delta["removed"]) == 64
    assert delta["added"] == []


def test_by_set_of_two_empty_trees_is_empty():
    delta = compute_delta_by_set(None, None)
    assert delta == {"added": [], "removed": [], "unchanged": []}


# ---------------------------------------------------------------------------
# The property the whole project rests on
# ---------------------------------------------------------------------------


def test_localized_edit_yields_small_delta():
    """Changing one chunk in a large file must produce a one-chunk delta.

    This is the claim that makes a 20 GB file cheap to version.
    """
    original = _hashes(10_000)
    updated = list(original)
    target = 5000
    updated[target] = hashlib.sha256(b"one localized edit").hexdigest()

    delta = compute_delta_by_set(build_tree(original), build_tree(updated))

    assert len(delta["added"]) == 1
    assert len(delta["removed"]) == 1
    assert len(delta["unchanged"]) == 9_999
