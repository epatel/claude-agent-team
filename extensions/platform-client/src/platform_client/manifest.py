"""Manifest sync: the code-transport primitive between lab and platform clients.

A *manifest* maps relative file paths to content hashes (sha256). The lab scans
a project's working tree and sends the manifest with a task; the client compares
it to its mirror, requests only the files that differ, and deletes the ones that
vanished. The same scan, diffed before/after a run, yields the changed-files
report. Identity of "what ran" is ``manifest_hash`` — a stable digest of the
whole tree — since there is no commit sha for uncommitted state.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# Directory/file names never synced (also never reported as run changes).
DEFAULT_IGNORES = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        "node_modules",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".DS_Store",
        ".lab-uploads",  # web-console chat attachments — lab-local scratch
    }
)

# Files larger than this are left out of the manifest (and so out of sync).
MAX_FILE_BYTES = 10_000_000

Manifest = dict[str, str]  # relative posix path -> sha256 hex


class PathOutsideRoot(ValueError):
    """A relative path would escape the sync root (absolute or ``..``)."""


def _resolve_inside(root: Path, relpath: str) -> Path:
    """``root / relpath``, refusing absolute paths and ``..`` escapes."""
    candidate = Path(relpath)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise PathOutsideRoot(relpath)
    return root / candidate


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan(root: Path, *, ignores: frozenset[str] = DEFAULT_IGNORES) -> Manifest:
    """Manifest of every regular file under ``root`` (ignored names and
    oversized files excluded). Paths are posix-style and root-relative."""
    manifest: Manifest = {}
    stack = [root]
    while stack:
        directory = stack.pop()
        for entry in directory.iterdir():
            if entry.name in ignores:
                continue
            if entry.is_symlink():
                continue  # never follow links out of the tree
            if entry.is_dir():
                stack.append(entry)
            elif entry.is_file() and entry.stat().st_size <= MAX_FILE_BYTES:
                manifest[entry.relative_to(root).as_posix()] = _hash_file(entry)
    return manifest


def manifest_hash(manifest: Manifest) -> str:
    """Stable digest of a whole tree — the run's identity in place of a sha."""
    digest = hashlib.sha256()
    for path in sorted(manifest):
        digest.update(path.encode())
        digest.update(b"\0")
        digest.update(manifest[path].encode())
        digest.update(b"\0")
    return digest.hexdigest()


def delta(source: Manifest, mirror: Manifest) -> tuple[list[str], list[str]]:
    """What the mirror must fetch and delete to match the source.

    Returns ``(need, delete)``: paths missing or stale in the mirror, and
    paths present in the mirror but gone from the source. Both sorted.
    """
    need = sorted(p for p, h in source.items() if mirror.get(p) != h)
    delete = sorted(p for p in mirror if p not in source)
    return need, delete


def changed(before: Manifest, after: Manifest) -> dict[str, list[str]]:
    """Per-run change report: files a command added, modified, or deleted."""
    return {
        "added": sorted(p for p in after if p not in before),
        "modified": sorted(p for p in after if p in before and before[p] != after[p]),
        "deleted": sorted(p for p in before if p not in after),
    }


def read_file(root: Path, relpath: str) -> bytes:
    return _resolve_inside(root, relpath).read_bytes()


def write_file(root: Path, relpath: str, data: bytes) -> None:
    target = _resolve_inside(root, relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def delete_files(root: Path, relpaths: list[str]) -> None:
    """Remove files from a mirror (missing ones are ignored; dirs left in place)."""
    for relpath in relpaths:
        _resolve_inside(root, relpath).unlink(missing_ok=True)
