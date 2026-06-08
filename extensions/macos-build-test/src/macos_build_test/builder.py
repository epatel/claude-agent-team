"""Build/test execution for the macOS extension (see cards/extension-clients.md).

Clones a git ref into a throwaway checkout and runs a command there, so the lab
can build/test the exact commit it produced without touching the lab host.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_MAX_OUTPUT = 20_000  # chars per stream, to keep tool results bounded


@dataclass
class CommandResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    ref: str
    command: str


def _truncate(text: str) -> str:
    return text if len(text) <= _MAX_OUTPUT else text[:_MAX_OUTPUT] + "\n...[truncated]"


def run_in_checkout(repo: str, ref: str, command: str, *, timeout: float = 600.0) -> CommandResult:
    """Clone ``repo``, check out ``ref``, run ``command`` there; return the result."""
    workdir = Path(tempfile.mkdtemp(prefix="ext-build-"))
    try:
        dest = workdir / "repo"
        clone = subprocess.run(
            ["git", "clone", "--quiet", repo, str(dest)], capture_output=True, text=True
        )
        if clone.returncode != 0:
            return CommandResult(False, clone.returncode, "", _truncate(clone.stderr), ref, command)

        checkout = subprocess.run(
            ["git", "-C", str(dest), "checkout", "--quiet", ref], capture_output=True, text=True
        )
        if checkout.returncode != 0:
            return CommandResult(
                False, checkout.returncode, "", _truncate(checkout.stderr), ref, command
            )

        try:
            proc = subprocess.run(
                command, shell=True, cwd=dest, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return CommandResult(False, 124, "", f"timed out after {timeout}s", ref, command)

        return CommandResult(
            proc.returncode == 0,
            proc.returncode,
            _truncate(proc.stdout),
            _truncate(proc.stderr),
            ref,
            command,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
