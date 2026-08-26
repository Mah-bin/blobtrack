"""
test_cli.py — Phase 1 CLI regression tests for Member 1

Covers:
- argparse parser creation and --help/--version
- all 8 commands recognized
- invalid command handling
- missing required arguments
- init creates .blobtrack structure
- init twice does not destroy
- init in different directories
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
# Stub tests - future commands must exit 1 with controlled message
# ---------------------------------------------------------------------------

class TestStubs:
    def test_add_stub_exits_1(self, capsys):
        from blobtrack.cli.commands import cmd_add

        with pytest.raises(SystemExit) as exc:
            cmd_add("somefile.bin")
        assert exc.value.code == 1

    def test_commit_stub_exits_1(self):
        from blobtrack.cli.commands import cmd_commit

        with pytest.raises(SystemExit) as exc:
            cmd_commit("hello")
        assert exc.value.code == 1

    def test_log_stub_exits_1(self):
        from blobtrack.cli.commands import cmd_log

        with pytest.raises(SystemExit) as exc:
            cmd_log()
        assert exc.value.code == 1

    def test_checkout_stub_exits_1(self):
        from blobtrack.cli.commands import cmd_checkout

        with pytest.raises(SystemExit) as exc:
            cmd_checkout("abc123")
        assert exc.value.code == 1

    def test_push_pull_gc_stubs(self):
        from blobtrack.cli.commands import cmd_push, cmd_pull, cmd_gc

        for fn, arg in [(cmd_push, "origin"), (cmd_pull, "origin"), (cmd_gc, None)]:
            with pytest.raises(SystemExit) as exc:
                if arg is None:
                    fn()
                else:
                    fn(arg)
            assert exc.value.code == 1
