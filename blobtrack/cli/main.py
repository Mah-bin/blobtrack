"""blob CLI entry point - argparse front door.

The primary command is ``blob``; ``blobtrack`` is kept as an alias so existing
scripts keep working.
"""

import argparse
import sys

from blobtrack import __version__
from blobtrack.cli import commands


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blob",
        description=(
            "Content-Aware Binary Version Control System - incremental "
            "versioning for massive binary files"
        ),
    )
    parser.add_argument("--version", action="version", version=f"blob {__version__}")

    sub = parser.add_subparsers(dest="cmd", required=True, title="commands", metavar="<command>")

    sub.add_parser("init", help="Create a new blob repository in the current directory")

    p_add = sub.add_parser("add", help="Chunk, hash and stage a file")
    p_add.add_argument("filepath", help="Path to file to add")

    p_commit = sub.add_parser("commit", help="Save a snapshot of tracked files")
    p_commit.add_argument("-m", "--message", required=True, help="Commit message")

    sub.add_parser("log", help="Show commit history, newest first")

    p_checkout = sub.add_parser("checkout", help="Restore a commit or branch into the working tree")
    p_checkout.add_argument("commit_hash", help="Commit hash (or abbreviation) or branch")

    p_rm = sub.add_parser("rm", help="Stop tracking a path")
    p_rm.add_argument("filepath", help="Path to stop tracking")
    p_rm.add_argument("-r", "--recursive", action="store_true", help="Accepted for familiarity")

    sub.add_parser("fsck", help="Verify repository integrity (missing and corrupt chunks)")

    p_gc = sub.add_parser("gc", help="Delete chunks that no commit references")
    p_gc.add_argument("--dry-run", action="store_true", help="Report what would be deleted")

    p_migrate = sub.add_parser("migrate", help="Move legacy flat chunks into the fan-out layout")
    p_migrate.add_argument("--dry-run", action="store_true", help="Report what would be moved")

    p_branch = sub.add_parser("branch", help="List branches, or create one")
    p_branch.add_argument("name", nargs="?", help="Branch to create")
    p_branch.add_argument("-d", "--delete", action="store_true", help="Delete a branch")

    p_switch = sub.add_parser("switch", help="Switch the current branch")
    p_switch.add_argument("branch", help="Branch to switch to")

    p_merge = sub.add_parser("merge", help="Merge a branch into the current one")
    p_merge.add_argument("branch", help="Branch to merge in")

    p_push = sub.add_parser("push", help="Push commits and chunks to a remote")
    p_push.add_argument("remote", nargs="?", default="origin", help="Remote path (default: origin)")

    p_pull = sub.add_parser("pull", help="Pull commits and chunks from a remote")
    p_pull.add_argument("remote", nargs="?", default="origin", help="Remote path (default: origin)")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.cmd == "init":
            commands.cmd_init()
        elif args.cmd == "add":
            commands.cmd_add(args.filepath)
        elif args.cmd == "commit":
            commands.cmd_commit(args.message)
        elif args.cmd == "log":
            commands.cmd_log()
        elif args.cmd == "checkout":
            commands.cmd_checkout(args.commit_hash)
        elif args.cmd == "rm":
            commands.cmd_rm(args.filepath, recursive=args.recursive)
        elif args.cmd == "fsck":
            commands.cmd_fsck()
        elif args.cmd == "gc":
            commands.cmd_gc(dry_run=args.dry_run)
        elif args.cmd == "migrate":
            commands.cmd_migrate(dry_run=args.dry_run)
        elif args.cmd == "branch":
            commands.cmd_branch(name=args.name, delete=args.delete)
        elif args.cmd == "switch":
            commands.cmd_switch(args.branch)
        elif args.cmd == "merge":
            commands.cmd_merge(args.branch)
        elif args.cmd == "push":
            commands.cmd_push(args.remote)
        elif args.cmd == "pull":
            commands.cmd_pull(args.remote)
        else:  # pragma: no cover - argparse rejects unknown commands first
            parser.print_help()
            sys.exit(1)
    except SystemExit:
        raise
    except Exception as exc:
        commands._print_error(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
