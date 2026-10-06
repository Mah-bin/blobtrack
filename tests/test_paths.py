"""Path containment and normalization tests.

These cover the security boundary: a tracked path must always stay inside the
repository, including when it arrives from a remote via pull.
"""

from pathlib import Path

import pytest

from blobtrack.storage.paths import (
    UnsafePathError,
    is_within,
    resolve_input_path,
    resolve_repo_root,
    safe_join,
    to_repo_relative,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".blobtrack" / "objects").mkdir(parents=True)
    (tmp_path / "sub").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# to_repo_relative
# ---------------------------------------------------------------------------


def test_converts_to_posix_relative(repo):
    assert to_repo_relative(repo / "sub" / "a.bin", repo) == "sub/a.bin"


def test_nested_dotdot_normalizes(repo):
    assert to_repo_relative(repo / "sub" / ".." / "a.bin", repo) == "a.bin"


def test_rejects_file_outside_repo(repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "evil.bin"
    outside.write_bytes(b"x")
    with pytest.raises(UnsafePathError):
        to_repo_relative(outside, repo)


def test_rejects_sibling_prefix_directory(repo, tmp_path_factory):
    """A sibling whose name merely starts with the repo name is not inside it."""
    sibling = repo.parent / (repo.name + "_evil")
    sibling.mkdir()
    target = sibling / "a.bin"
    target.write_bytes(b"x")
    with pytest.raises(UnsafePathError):
        to_repo_relative(target, repo)


# ---------------------------------------------------------------------------
# safe_join  (the checkout write guard)
# ---------------------------------------------------------------------------


def test_safe_join_resolves_inside(repo):
    assert safe_join(repo, "sub/a.bin") == (repo / "sub" / "a.bin").resolve()


def test_safe_join_accepts_nested(repo):
    assert safe_join(repo, "a/b/c.bin") == (repo / "a" / "b" / "c.bin").resolve()


@pytest.mark.parametrize(
    "hostile",
    [
        "../escape.bin",
        "../../escape.bin",
        "sub/../../escape.bin",
        "a/../../../escape.bin",
        "/etc/passwd",
        "/tmp/absolute.bin",
        "..",
    ],
)
def test_safe_join_rejects_escapes(repo, hostile):
    with pytest.raises(UnsafePathError):
        safe_join(repo, hostile)


@pytest.mark.parametrize("hostile", ["C:/Windows/win.ini", "C:\\Windows\\win.ini"])
def test_safe_join_rejects_drive_letters(repo, hostile):
    with pytest.raises(UnsafePathError):
        safe_join(repo, hostile)


def test_safe_join_rejects_leading_separator(repo):
    with pytest.raises(UnsafePathError):
        safe_join(repo, "\\absolute\\path.bin")


# ---------------------------------------------------------------------------
# resolve_repo_root / resolve_input_path / is_within
# ---------------------------------------------------------------------------


def test_resolve_repo_root_walks_up(repo, monkeypatch):
    nested = repo / "sub" / "deeper"
    nested.mkdir(parents=True)
    assert resolve_repo_root(nested) == repo.resolve()


def test_resolve_repo_root_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_repo_root(tmp_path) is None


def test_resolve_input_path_relative_to_cwd(repo, monkeypatch):
    monkeypatch.chdir(repo)
    assert resolve_input_path("a.bin") == (repo / "a.bin").resolve()


def test_is_within(repo):
    assert is_within(repo / "sub", repo) is True
    assert is_within(repo.parent / "elsewhere", repo) is False
