"""
test_cli.py — Phase 1-3 CLI regression tests for Member 1

Covers:
- argparse parser creation and --help/--version
- all 8 commands recognized
- invalid command handling
- missing required arguments
- init creates .blobtrack structure
- init twice does not destroy
- init in different directories
- add integration (Phase 2) + commit integration (Phase 3)
- future commands are stubbed (not yet implemented)
"""

import pathlib
import subprocess
import sys

import pytest

from blobtrack.cli.main import build_parser
from blobtrack.cli.commands import cmd_init


# ---------------------------------------------------------------------------
# Parser / help tests
# ---------------------------------------------------------------------------

class TestParser:
    def test_parser_has_all_commands(self):
        parser = build_parser()
        # Collect subparser choices
        actions = [a for a in parser._actions if isinstance(a, type(parser._actions[0]))]
        # Simpler: try parsing each command
        for cmd in ["init", "add", "commit", "log", "checkout", "push", "pull", "gc"]:
            # Build minimal valid args for each to ensure parser accepts the command
            if cmd == "add":
                args = parser.parse_args([cmd, "somefile.bin"])
                assert args.cmd == "add"
            elif cmd == "commit":
                args = parser.parse_args([cmd, "-m", "msg"])
                assert args.cmd == "commit"
            elif cmd == "checkout":
                args = parser.parse_args([cmd, "abc123"])
                assert args.cmd == "checkout"
            elif cmd in ("push", "pull"):
                args = parser.parse_args([cmd, "origin"])
                assert args.cmd == cmd
            else:
                args = parser.parse_args([cmd])
                assert args.cmd == cmd

    def test_init_no_args_parses(self):
        parser = build_parser()
        args = parser.parse_args(["init"])
        assert args.cmd == "init"

    def test_add_requires_filepath(self):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["add"])
        assert exc.value.code == 2

    def test_commit_requires_message(self):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["commit"])
        assert exc.value.code == 2

        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["commit", "-m"])
        assert exc.value.code == 2

    def test_checkout_requires_hash(self):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["checkout"])
        assert exc.value.code == 2

    def test_invalid_command(self):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["banana"])
        assert exc.value.code == 2

    def test_no_command_fails(self):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([])
        assert exc.value.code == 2

    def test_commit_message_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["commit", "-m", "first version"])
        assert args.message == "first version"

    def test_checkout_hash_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["checkout", "deadbeef123"])
        assert args.commit_hash == "deadbeef123"

    def test_push_pull_default_origin(self):
        parser = build_parser()
        args = parser.parse_args(["push"])
        assert args.remote == "origin"
        args = parser.parse_args(["pull"])
        assert args.remote == "origin"
        args = parser.parse_args(["push", "origin"])
        assert args.remote == "origin"
        args = parser.parse_args(["push", "myremote"])
        assert args.remote == "myremote"


class TestCLIHelp:
    def test_help_via_subprocess(self):
        result = subprocess.run(
            [sys.executable, "-m", "blobtrack.cli.main", "--help"],
            capture_output=True,
            text=True,
        )
        # Should exit 0 and list all 8 commands
        assert result.returncode == 0
        for cmd in ["init", "add", "commit", "log", "checkout", "push", "pull", "gc"]:
            assert cmd in result.stdout

    def test_version_via_subprocess(self):
        result = subprocess.run(
            [sys.executable, "-m", "blobtrack.cli.main", "--version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "blobtrack" in result.stdout.lower() or "blobtrack" in result.stderr.lower()


# ---------------------------------------------------------------------------
# cmd_init tests
# ---------------------------------------------------------------------------

class TestCmdInit:
    def test_init_creates_structure(self, tmp_path):
        # tmp_path is a fresh empty directory provided by pytest
        cmd_init(cwd=tmp_path)
        assert (tmp_path / ".blobtrack").is_dir()
        assert (tmp_path / ".blobtrack" / "objects").is_dir()
        assert (tmp_path / ".blobtrack" / "commits").is_dir()
        # index.db exists (created and initialized with schema)
        assert (tmp_path / ".blobtrack" / "index.db").stat().st_size >= 0

    def test_init_twice_does_not_destroy(self, tmp_path):
        cmd_init(cwd=tmp_path)
        # Create a marker file inside objects to ensure second init doesn't delete
        marker = tmp_path / ".blobtrack" / "objects" / "keep_me"
        marker.write_text("preserve")
        with pytest.raises(SystemExit) as exc:
            cmd_init(cwd=tmp_path)
        assert exc.value.code == 1
        # Structure still exists and marker preserved
        assert (tmp_path / ".blobtrack").is_dir()
        assert marker.exists()
        assert marker.read_text() == "preserve"

    def test_init_in_different_directories(self, tmp_path):
        dir_a = tmp_path / "A"
        dir_b = tmp_path / "B"
        dir_a.mkdir()
        dir_b.mkdir()
        cmd_init(cwd=dir_a)
        cmd_init(cwd=dir_b)
        assert (dir_a / ".blobtrack" / "index.db").is_file()
        assert (dir_b / ".blobtrack" / "index.db").is_file()
        # They are separate
        assert (dir_a / ".blobtrack").resolve() != (dir_b / ".blobtrack").resolve()

    def test_init_does_not_create_outside_cwd(self, tmp_path):
        # Ensure init only affects cwd, not parent
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        cmd_init(cwd=subdir)
        assert (subdir / ".blobtrack").is_dir()
        assert not (tmp_path / ".blobtrack").exists()


# ---------------------------------------------------------------------------
# Phase 2 - add integration
# ---------------------------------------------------------------------------

class TestAddIntegration:
    def test_add_basic(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd_init(cwd=tmp_path)
        f = tmp_path / "a.bin"
        f.write_bytes(b"hello add test " * 5000)
        from blobtrack.cli.commands import cmd_add
        cmd_add(str(f))
        # Verify objects and DB
        from blobtrack.storage.index_db import IndexDB
        from blobtrack.storage.local_store import LocalStore
        assert len(LocalStore(tmp_path / ".blobtrack" / "objects").list_chunks()) >= 1
        db = IndexDB(tmp_path / ".blobtrack" / "index.db")
        assert len(db.list_files()) == 1
        assert len(db.list_chunks()) >= 1
        db.close()

    def test_add_deduplication(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd_init(cwd=tmp_path)
        f = tmp_path / "dup.bin"
        f.write_bytes(b"dedup content " * 8000)
        from blobtrack.cli.commands import cmd_add
        from blobtrack.storage.local_store import LocalStore
        cmd_add(str(f))
        c1 = len(LocalStore(tmp_path / ".blobtrack" / "objects").list_chunks())
        cmd_add(str(f))
        c2 = len(LocalStore(tmp_path / ".blobtrack" / "objects").list_chunks())
        assert c1 == c2  # no duplicate objects

    def test_add_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd_init(cwd=tmp_path)
        from blobtrack.cli.commands import cmd_add
        with pytest.raises(SystemExit) as exc:
            cmd_add(str(tmp_path / "missing.bin"))
        assert exc.value.code == 1

    def test_add_requires_repo(self, tmp_path, monkeypatch):
        # tmp_path has no .blobtrack
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "x.bin"
        f.write_bytes(b"data")
        from blobtrack.cli.commands import cmd_add
        with pytest.raises(SystemExit) as exc:
            cmd_add(str(f))
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Phase 3 - commit integration
# ---------------------------------------------------------------------------

class TestCommitIntegration:
    def test_commit_without_repo(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from blobtrack.cli.commands import cmd_commit
        with pytest.raises(SystemExit) as exc:
            cmd_commit("msg")
        assert exc.value.code == 1

    def test_commit_without_add(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd_init(cwd=tmp_path)
        from blobtrack.cli.commands import cmd_commit
        with pytest.raises(SystemExit) as exc:
            cmd_commit("no files")
        assert exc.value.code == 1

    def test_first_commit(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd_init(cwd=tmp_path)
        f = tmp_path / "file.bin"
        f.write_bytes(b"first version content " * 4000)
        from blobtrack.cli.commands import cmd_add, cmd_commit
        cmd_add(str(f))
        cmd_commit("first commit")
        from blobtrack.storage.index_db import IndexDB
        db = IndexDB(tmp_path / ".blobtrack" / "index.db")
        commits = db.list_commits()
        assert len(commits) == 1
        assert commits[0]["message"] == "first commit"
        assert commits[0]["parent_hash"] is None
        assert commits[0]["merkle_root_hash"] is not None
        assert len(db.get_commit_chunk_refs(commits[0]["commit_hash"])) >= 1
        db.close()

    def test_second_commit_has_parent(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd_init(cwd=tmp_path)
        f = tmp_path / "file.bin"
        f.write_bytes(b"content v1 " * 5000)
        from blobtrack.cli.commands import cmd_add, cmd_commit
        cmd_add(str(f))
        cmd_commit("v1")
        from blobtrack.storage.index_db import IndexDB
        db = IndexDB(tmp_path / ".blobtrack" / "index.db")
        c1 = db.list_commits()[0]["commit_hash"]
        db.close()
        # Second commit same file
        cmd_add(str(f))
        cmd_commit("v2")
        db = IndexDB(tmp_path / ".blobtrack" / "index.db")
        commits = db.list_commits()
        assert len(commits) == 2
        # Newest first
        newest = commits[0]
        assert newest["parent_hash"] == c1
        assert newest["message"] == "v2"
        db.close()

    def test_commit_modified_changes_root(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd_init(cwd=tmp_path)
        f = tmp_path / "file.bin"
        f.write_bytes(b"A" * (2 * 1024 * 1024))  # 2MB single chunk
        from blobtrack.cli.commands import cmd_add, cmd_commit
        cmd_add(str(f))
        cmd_commit("v1")
        from blobtrack.storage.index_db import IndexDB
        db = IndexDB(tmp_path / ".blobtrack" / "index.db")
        r1 = db.list_commits()[0]["merkle_root_hash"]
        db.close()
        # Modify
        f.write_bytes(b"B" * (2 * 1024 * 1024))
        cmd_add(str(f))
        cmd_commit("v2 modified")
        db = IndexDB(tmp_path / ".blobtrack" / "index.db")
        r2 = db.list_commits()[0]["merkle_root_hash"]
        assert r1 != r2
        db.close()

    def test_commit_empty_message_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd_init(cwd=tmp_path)
        from blobtrack.cli.commands import cmd_commit
        with pytest.raises(SystemExit) as exc:
            cmd_commit("")
        assert exc.value.code == 1
        with pytest.raises(SystemExit) as exc:
            cmd_commit("   ")
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Phase 4 - log / checkout / gc integration
# ---------------------------------------------------------------------------

class TestLogIntegration:
    def test_log_without_repo(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from blobtrack.cli.commands import cmd_log
        with pytest.raises(SystemExit) as exc:
            cmd_log()
        assert exc.value.code == 1

    def test_log_empty(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        cmd_init(cwd=tmp_path)
        from blobtrack.cli.commands import cmd_log
        # Should not exit, just print No commits
        cmd_log()
        captured = capsys.readouterr()
        assert "No commits" in captured.out or "No commits" in captured.err or "0" in captured.out

    def test_log_one_and_multiple(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd_init(cwd=tmp_path)
        f = tmp_path / "f.bin"
        f.write_bytes(b"log test " * 2000)
        from blobtrack.cli.commands import cmd_add, cmd_commit, cmd_log
        cmd_add(str(f))
        cmd_commit("first log")
        from blobtrack.storage.index_db import IndexDB
        db = IndexDB(tmp_path / ".blobtrack" / "index.db")
        assert len(db.list_commits()) == 1
        db.close()
        # Add second commit
        f.write_bytes(b"log test v2 " * 2000)
        cmd_add(str(f))
        cmd_commit("second log")
        db = IndexDB(tmp_path / ".blobtrack" / "index.db")
        commits = db.list_commits()
        assert len(commits) == 2
        assert commits[0]["message"] == "second log"  # newest first
        db.close()
        # Should not raise
        cmd_log()


class TestCheckoutIntegration:
    def test_checkout_requires_repo(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from blobtrack.cli.commands import cmd_checkout
        with pytest.raises(SystemExit) as exc:
            cmd_checkout("abc123")
        assert exc.value.code == 1

    def test_checkout_invalid_hash(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd_init(cwd=tmp_path)
        from blobtrack.cli.commands import cmd_checkout
        with pytest.raises(SystemExit) as exc:
            cmd_checkout("zzzzzz")  # not hex
        assert exc.value.code == 1
        with pytest.raises(SystemExit) as exc:
            cmd_checkout("deadbeef")  # not found
        assert exc.value.code == 1

    def test_checkout_restores_exact(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd_init(cwd=tmp_path)
        f = tmp_path / "orig.bin"
        orig = b"checkout exact bytes " * 3000
        f.write_bytes(orig)
        import hashlib
        orig_hash = hashlib.sha256(orig).hexdigest()
        from blobtrack.cli.commands import cmd_add, cmd_commit, cmd_checkout
        cmd_add(str(f))
        cmd_commit("v1")
        from blobtrack.storage.index_db import IndexDB
        db = IndexDB(tmp_path / ".blobtrack" / "index.db")
        c1 = db.list_commits()[0]["commit_hash"]
        db.close()
        # Modify
        f.write_bytes(b"modified " * 4000)
        cmd_add(str(f))
        cmd_commit("v2")
        # Checkout v1 -> must restore orig
        cmd_checkout(c1)
        restored = f.read_bytes()
        assert hashlib.sha256(restored).hexdigest() == orig_hash
        assert restored == orig

    def test_checkout_multi_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd_init(cwd=tmp_path)
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_bytes(b"A" * 8000)
        b.write_bytes(b"B" * 9000)
        from blobtrack.cli.commands import cmd_add, cmd_commit, cmd_checkout
        cmd_add(str(a))
        cmd_add(str(b))
        cmd_commit("two files")
        from blobtrack.storage.index_db import IndexDB
        db = IndexDB(tmp_path / ".blobtrack" / "index.db")
        c1 = db.list_commits()[0]["commit_hash"]
        db.close()
        # Modify only a
        a.write_bytes(b"AX" * 4000)
        cmd_add(str(a))
        cmd_commit("modify a")
        # Checkout first commit -> both files restored
        cmd_checkout(c1)
        assert a.read_bytes() == b"A" * 8000
        assert b.read_bytes() == b"B" * 9000


class TestGCIntegration:
    def test_gc_no_orphans(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd_init(cwd=tmp_path)
        f = tmp_path / "g.bin"
        f.write_bytes(b"gc test " * 3000)
        from blobtrack.cli.commands import cmd_add, cmd_commit, cmd_gc
        cmd_add(str(f))
        cmd_commit("v1")
        # No orphans yet
        cmd_gc()  # should report no orphans, not fail
        from blobtrack.storage.local_store import LocalStore
        from blobtrack.storage.index_db import IndexDB
        assert len(LocalStore(tmp_path / ".blobtrack" / "objects").list_chunks()) >= 1
        db = IndexDB(tmp_path / ".blobtrack" / "index.db")
        assert len(db.get_orphan_chunks()) == 0
        db.close()

    def test_gc_deletes_orphan(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd_init(cwd=tmp_path)
        f = tmp_path / "g2.bin"
        f.write_bytes(b"gc orphan " * 2000)
        from blobtrack.cli.commands import cmd_add, cmd_commit, cmd_gc
        cmd_add(str(f))
        cmd_commit("v1")
        from blobtrack.storage.local_store import LocalStore
        ls = LocalStore(tmp_path / ".blobtrack" / "objects")
        # Create orphan directly
        ls.store_chunk("deadbeef" * 8, b"orphan")
        assert ls.has_chunk("deadbeef" * 8)
        cmd_gc()
        assert not ls.has_chunk("deadbeef" * 8)

    def test_gc_requires_repo(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from blobtrack.cli.commands import cmd_gc
        with pytest.raises(SystemExit) as exc:
            cmd_gc()
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Phase 5 - push/pull are now implemented (no longer stubs)
# Comprehensive tests in test_remote_cli.py
# ---------------------------------------------------------------------------

class TestPushPullImplemented:
    def test_push_requires_repo(self, tmp_path, monkeypatch):
        """Push outside a repo exits 1 with controlled error."""
        monkeypatch.chdir(tmp_path)
        from blobtrack.cli.commands import cmd_push
        with pytest.raises(SystemExit) as exc:
            cmd_push(str(tmp_path / "some_remote"))
        assert exc.value.code == 1

    def test_pull_requires_repo(self, tmp_path, monkeypatch):
        """Pull outside a repo exits 1 with controlled error."""
        monkeypatch.chdir(tmp_path)
        from blobtrack.cli.commands import cmd_pull
        with pytest.raises(SystemExit) as exc:
            cmd_pull(str(tmp_path / "some_remote"))
        assert exc.value.code == 1

