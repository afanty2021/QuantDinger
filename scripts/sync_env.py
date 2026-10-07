#!/usr/bin/env python3
"""Merge missing dotenv entries without changing existing configuration."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_SOURCE = Path(__file__).resolve().parents[1] / "backend_api_python"
if str(BACKEND_SOURCE) not in sys.path:
    sys.path.insert(0, str(BACKEND_SOURCE))

from env_sync_core import entries as _entries
from env_sync_core import sync_env


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Append missing dotenv keys while preserving every existing value, "
            "comment, and unknown setting."
        )
    )
    parser.add_argument("--env-file", default=".env", type=Path)
    parser.add_argument("--template", default=".env.example", type=Path)
    parser.add_argument(
        "--legacy",
        action="append",
        default=[],
        type=Path,
        help="Import missing keys from an older env file before the template.",
    )
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit with status 1 when keys are missing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    dry_run = args.dry_run or args.check
    try:
        missing, backup_path, created = sync_env(
            args.env_file,
            args.template,
            args.legacy,
            backup=args.backup,
            dry_run=dry_run,
        )
    except (OSError, ValueError) as exc:
        print(f"Environment sync failed: {exc}", file=sys.stderr)
        return 2

    if created:
        verb = "Would create" if dry_run else "Created"
        print(f"{verb} environment file: {args.env_file}")
    if not missing:
        if not created:
            print(f"Environment is up to date: {args.env_file}")
        return 1 if args.check and created else 0

    action = "Would add" if dry_run else "Added"
    print(f"{action} {len(missing)} missing key(s) to {args.env_file}:")
    print("  " + ", ".join(entry.key for entry in missing))
    if backup_path is not None:
        print(f"Backup created: {backup_path}")
    if args.check:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
