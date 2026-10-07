"""Merge missing dotenv entries without changing existing configuration."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ASSIGNMENT_RE = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?)"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=.*$"
)


@dataclass(frozen=True)
class EnvEntry:
    key: str
    line: str
    source: Path


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def entries(path: Path) -> list[EnvEntry]:
    if not path.exists():
        return []
    result: list[EnvEntry] = []
    for raw_line in read_text(path).splitlines():
        match = ASSIGNMENT_RE.match(raw_line)
        if match:
            result.append(EnvEntry(match.group("key"), raw_line, path))
    return result


def existing_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for line in text.splitlines():
        match = ASSIGNMENT_RE.match(line)
        if match:
            keys.add(match.group("key"))
    return keys


def missing_entries(current_text: str, sources: Iterable[Path]) -> list[EnvEntry]:
    known = existing_keys(current_text)
    missing: list[EnvEntry] = []
    for source in sources:
        for entry in entries(source):
            if entry.key in known:
                continue
            known.add(entry.key)
            missing.append(entry)
    return missing


def append_entries(current_text: str, new_entries: list[EnvEntry]) -> str:
    if not new_entries:
        return current_text
    chunks: list[str] = []
    if current_text:
        chunks.extend((current_text.rstrip("\r\n"), ""))
    chunks.append("# Added automatically by QuantDinger environment sync")
    previous_source: Path | None = None
    for entry in new_entries:
        if entry.source != previous_source:
            chunks.append(f"# Missing entries imported from {entry.source.as_posix()}")
            previous_source = entry.source
        chunks.append(entry.line)
    return "\n".join(chunks) + "\n"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def sync_env(
    env_file: Path,
    template: Path,
    legacy_files: Iterable[Path] = (),
    *,
    backup: bool = False,
    dry_run: bool = False,
) -> tuple[list[EnvEntry], Path | None, bool]:
    if not template.is_file():
        raise FileNotFoundError(f"Template not found: {template}")
    env_exists = env_file.exists()
    legacy_sources = [path for path in legacy_files if path.is_file()]
    created = not env_exists
    if env_exists:
        current_text = read_text(env_file)
    elif legacy_sources:
        current_text = read_text(legacy_sources.pop(0))
    else:
        current_text = read_text(template)
    missing = missing_entries(current_text, [*legacy_sources, template])
    if dry_run:
        return missing, None, created
    if not missing and not created:
        return missing, None, False
    backup_path: Path | None = None
    if backup and env_file.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = env_file.with_name(f"{env_file.name}.bak.{stamp}")
        suffix = 1
        while backup_path.exists():
            backup_path = env_file.with_name(f"{env_file.name}.bak.{stamp}.{suffix}")
            suffix += 1
        shutil.copy2(env_file, backup_path)
    atomic_write(env_file, append_entries(current_text, missing))
    return missing, backup_path, created
