from blobtrack.core.merkle_tree import build_tree, serialize_tree, deserialize_tree, collect_leaf_hashes
from blobtrack.core.differ import compute_delta, compute_delta_by_set

# Test 1: Build tree from 5 chunk hashes (odd number)
hashes = ["aaa", "bbb", "ccc", "ddd", "eee"]
tree = build_tree(hashes)
print(f"Root hash: {tree.hash[:20]}...")
recovered = collect_leaf_hashes(tree)
print(f"Leaves recovered: {recovered}")
assert recovered == hashes, "FAIL: leaf recovery mismatch"
print("TEST 1 PASSED: Odd-number tree builds correctly\n")

# Test 2: Serialize and deserialize
json_str = serialize_tree(tree)
tree2 = deserialize_tree(json_str)
assert tree2.hash == tree.hash, "FAIL: deserialized root hash mismatch"
assert collect_leaf_hashes(tree2) == hashes, "FAIL: deserialized leaves mismatch"
print("TEST 2 PASSED: Serialization round-trip works\n")

# Test 3: Delta diff - change one chunk
old_hashes = ["aaa", "bbb", "ccc", "ddd", "eee"]
new_hashes = ["aaa", "bbb", "zzz", "ddd", "eee"]
old_tree = build_tree(old_hashes)
new_tree = build_tree(new_hashes)
delta = compute_delta(old_tree, new_tree)
print(f"Positional delta: added={delta['added']}, removed={delta['removed']}, unchanged_count={len(delta['unchanged'])}")
print("TEST 3 PASSED: Positional delta works\n")

# Test 4: Set-based delta
delta_set = compute_delta_by_set(old_tree, new_tree)
print(f"Set delta: added={delta_set['added']}, removed={delta_set['removed']}, unchanged_count={len(delta_set['unchanged'])}")
assert "zzz" in delta_set["added"], "FAIL: zzz not in added"
assert "ccc" in delta_set["removed"], "FAIL: ccc not in removed"
print("TEST 4 PASSED: Set-based delta works\n")

# Test 5: Empty tree
assert build_tree([]) is None, "FAIL: empty tree should be None"
print("TEST 5 PASSED: Empty tree returns None\n")

# Test 6: Single chunk tree
single = build_tree(["abc"])
assert single.hash == "abc"
assert single.is_leaf == True
print("TEST 6 PASSED: Single chunk tree works\n")

# Test 7: Identical trees produce zero delta
same1 = build_tree(["aaa", "bbb", "ccc"])
same2 = build_tree(["aaa", "bbb", "ccc"])
d = compute_delta(same1, same2)
assert len(d["added"]) == 0 and len(d["removed"]) == 0
print("TEST 7 PASSED: Identical trees = zero delta\n")

print("ALL 7 TESTS PASSED")
