"""blobtrack CLI entry point - argparse front door.

Member 1 - CLI & Integration Lead
Phase 1: registers all 8 commands, dispatches to commands.py
Future commands (add/commit/log/etc) are parsed but stubbed until later increments.
"""

import argparse
import sys

from blobtrack import __version__
from blobtrack.cli import commands


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blobtrack",
        description="Content-Aware Binary Version Control System - incremental versioning for massive binary files",
    )
    parser.add_argument("--version", action="version", version=f"blobtrack {__version__}")

    sub = parser.add_subparsers(dest="cmd", required=True, title="commands", metavar="<command>")

    # init - no args
    sub.add_parser("init", help="Create a new blobtrack repository in the current directory")

    # add - requires filepath
    p_add = sub.add_parser("add", help="Chunk, hash and stage a file (Increment 2)")
    p_add.add_argument("filepath", help="Path to file to add")

    # commit - requires -m message
    p_commit = sub.add_parser("commit", help="Save a snapshot (Increment 3)")
    p_commit.add_argument("-m", "--message", required=True, help="Commit message")

    # log - no args
    sub.add_parser("log", help="Show commit history (Increment 4)")

    # checkout - requires commit hash
    p_checkout = sub.add_parser("checkout", help="Restore a previous version (Increment 4)")
    p_checkout.add_argument("commit_hash", help="Commit hash to checkout")

    # push - requires remote
    p_push = sub.add_parser("push", help="Push delta chunks to remote (Increment 5)")
    p_push.add_argument("remote", nargs="?", default="origin", help="Remote name or path (default: origin)")

    # pull - requires remote
    p_pull = sub.add_parser("pull", help="Pull delta chunks from remote (Increment 5)")
    p_pull.add_argument("remote", nargs="?", default="origin", help="Remote name or path (default: origin)")

    # gc - no args
    sub.add_parser("gc", help="Garbage collect orphan chunks (Increment 4)")

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
        elif args.cmd == "push":
            commands.cmd_push(args.remote)
        elif args.cmd == "pull":
            commands.cmd_pull(args.remote)
        elif args.cmd == "gc":
            commands.cmd_gc()
        else:
            parser.print_help()
            sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
