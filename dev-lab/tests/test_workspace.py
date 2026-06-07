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
