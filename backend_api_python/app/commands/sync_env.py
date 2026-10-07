"""Synchronize the unified project environment before services start."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from env_sync_core import sync_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path("/workspace"))
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    env_file = workspace / ".env"
    template = workspace / ".env.example"
    legacy_files = (
        workspace / "backend_api_python" / ".env",
        workspace / "backend.env",
    )
    try:
        missing, backup_path, created = sync_env(
            env_file,
            template,
            legacy_files,
            backup=True,
        )
    except (OSError, ValueError) as exc:
        print(f"Environment sync failed: {exc}", file=sys.stderr)
        return 2

    if created:
        print(f"Created unified environment file: {env_file}")
    if missing:
        print(f"Added {len(missing)} missing environment key(s).")
    else:
        print("Unified environment is up to date.")
    if backup_path:
        print(f"Backup created: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
