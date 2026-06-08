import subprocess
from pathlib import Path

import pytest
from dev_lab.workspace import Workspace, WorkspaceError


def _init_repo(path: Path) -> Workspace:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "lab@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Lab"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return Workspace(path)


def test_ensure_repo_rejects_non_repo(tmp_path):
    with pytest.raises(WorkspaceError):
        Workspace(tmp_path).ensure_repo()


def test_branch_exists_and_merge(tmp_path):
    ws = _init_repo(tmp_path)
    base = ws.current_branch()
    assert ws.default_branch() == base
    assert not ws.branch_exists("chat/x")

    ws.create_branch("chat/x")
    (tmp_path / "feature.txt").write_text("hi\n")
    ws.commit_all("add feature")
    assert ws.branch_exists("chat/x")

    merged = ws.merge(base, "chat/x", message="merge chat/x")
    assert merged
    assert ws.current_branch() == base
    # base now contains the feature from the chat branch
    got = subprocess.run(
        ["git", "-C", str(tmp_path), "cat-file", "-e", f"{base}:feature.txt"]
    ).returncode
    assert got == 0


def test_head_branch_and_commit(tmp_path):
    ws = _init_repo(tmp_path)
    ws.ensure_repo()

    base = ws.head_sha()
    assert base
    assert not ws.is_dirty()

    ws.create_branch("lab/test")
    assert ws.current_branch() == "lab/test"

    (tmp_path / "new.txt").write_text("hello\n")
    assert ws.is_dirty()

    sha = ws.commit_all("add new.txt")
    assert sha != base
    assert not ws.is_dirty()
