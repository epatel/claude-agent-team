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


def _init_bare_origin(path: Path) -> Path:
    """Create a bare repo with a couple of branches to act as ``origin``."""
    origin = path / "origin.git"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    seed = path / "seed"
    seed.mkdir()
    ws = _init_repo(seed)
    base = ws.current_branch()
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "origin", base], cwd=seed, check=True)
    # a branch that will only ever exist on origin
    subprocess.run(["git", "checkout", "-q", "-b", "develop"], cwd=seed, check=True)
    (seed / "dev.txt").write_text("dev\n")
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "dev"], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "origin", "develop"], cwd=seed, check=True)
    return origin


def _clone(origin: Path, dest: Path) -> Workspace:
    subprocess.run(["git", "clone", "-q", str(origin), str(dest)], check=True)
    subprocess.run(["git", "config", "user.email", "lab@example.com"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.name", "Lab"], cwd=dest, check=True)
    return Workspace(dest)


def test_list_branches_dedupes_local_and_remote(tmp_path):
    origin = _init_bare_origin(tmp_path)
    ws = _clone(origin, tmp_path / "clone")
    base = ws.current_branch()  # checked-out default branch (local + origin)

    branches = ws.list_branches()
    assert "develop" in branches  # exists only as origin/develop locally
    assert base in branches
    assert branches == sorted(branches)
    assert len(branches) == len(set(branches))  # deduped
    assert not any(b.startswith("origin/") or b.endswith("HEAD") for b in branches)
    assert not ws.branch_exists("develop")  # still only remote-tracking


def test_create_branch_off_remote_only_base(tmp_path):
    origin = _init_bare_origin(tmp_path)
    ws = _clone(origin, tmp_path / "clone")

    ws.create_branch("chat/feature", base="develop")
    assert ws.current_branch() == "chat/feature"
    # base was materialised as a local tracking branch
    assert ws.branch_exists("develop")
    # the new branch was cut from develop, so dev.txt is present
    assert (tmp_path / "clone" / "dev.txt").exists()


def test_create_branch_parent_is_chosen_base(tmp_path):
    """A new branch is rooted on the chosen base's tip, not the current HEAD.

    Uses the origin-only ``develop`` base: its first commit added ``dev.txt`` on
    top of the default branch, so a branch cut from it must have ``develop``'s
    tip as the parent of its first commit.
    """
    origin = _init_bare_origin(tmp_path)
    clone = tmp_path / "clone"
    ws = _clone(origin, clone)
    base_tip = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "origin/develop"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    ws.create_branch("chat/x", base="develop")
    (clone / "work.txt").write_text("work\n")
    ws.commit_all("work on chat branch")

    parent = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "chat/x^"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert parent == base_tip  # the chat branch grew from the chosen base


def test_create_branch_off_local_base(tmp_path):
    ws = _init_repo(tmp_path)
    base = ws.current_branch()
    ws.create_branch("chat/x", base=base)
    assert ws.current_branch() == "chat/x"


def test_create_branch_unknown_base_raises(tmp_path):
    ws = _init_repo(tmp_path)
    with pytest.raises(WorkspaceError):
        ws.create_branch("chat/x", base="nope")


def test_create_branch_default_keeps_current_head(tmp_path):
    ws = _init_repo(tmp_path)
    head = ws.head_sha()
    ws.create_branch("chat/here")
    assert ws.current_branch() == "chat/here"
    assert ws.head_sha() == head  # branched off current HEAD


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
