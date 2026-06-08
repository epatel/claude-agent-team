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


def test_commit_diff_and_subject(tmp_path):
    ws = _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("line one\n")
    sha = ws.commit_all("add a.txt")

    assert ws.commit_subject(sha) == "add a.txt"
    diff = ws.commit_diff(sha)
    assert "a.txt" in diff and "+line one" in diff

    # the seed (root) commit diffs cleanly too
    root = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert "README.md" in ws.commit_diff(root)

    with pytest.raises(WorkspaceError):
        ws.commit_diff("not-a-sha")


def test_list_tree_and_read_file(tmp_path):
    ws = _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n")

    top = {e["name"]: e for e in ws.list_tree()}
    assert top["src"]["type"] == "dir" and top["README.md"]["type"] == "file"
    assert ".git" not in top  # never exposed

    sub = ws.list_tree("src")
    assert sub[0]["name"] == "main.py" and sub[0]["path"] == "src/main.py"

    f = ws.read_file("src/main.py")
    assert f["binary"] is False and f["content"] == "print('hi')\n"

    # binary content is flagged, not returned raw
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\xff")
    blob = ws.read_file("blob.bin")
    assert blob["binary"] is True and blob["content"] == ""


def test_diff_status_against_base(tmp_path):
    """diff_status flags working-tree divergence from base in every direction.

    Covers the stale-checkout case (a file on base missing from the parked
    branch surfaces as ``deleted``), plus committed-on-branch, modified, and
    untracked changes.
    """
    ws = _init_repo(tmp_path)
    base = ws.current_branch()

    # On base, add a second file the stale branch will end up missing.
    initial = ws.head_sha()
    (tmp_path / "CLAUDE.md").write_text("guide\n")
    ws.commit_all("add CLAUDE.md")

    # A clean checkout of base diverges from base in no way.
    assert ws.diff_status(base) == {}

    # Park on an old branch cut from the initial commit (lacks CLAUDE.md),
    # commit a new file, edit a tracked one, and drop an untracked file.
    subprocess.run(
        ["git", "-C", str(tmp_path), "checkout", "-q", "-b", "chat/stale", initial],
        check=True,
    )
    (tmp_path / "feature.txt").write_text("hi\n")
    ws.commit_all("add feature.txt")
    (tmp_path / "README.md").write_text("seed\nmore\n")  # modified, uncommitted
    (tmp_path / "scratch.tmp").write_text("temp\n")  # untracked

    status = ws.diff_status(base)
    assert status["CLAUDE.md"] == "deleted"  # on base, absent from this checkout
    assert status["feature.txt"] == "new"  # committed on the branch, not on base
    assert status["README.md"] == "modified"  # uncommitted edit
    assert status["scratch.tmp"] == "new"  # untracked


def test_file_path_resolves_and_guards(tmp_path):
    ws = _init_repo(tmp_path)
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n")

    resolved = ws.file_path("assets/logo.png")
    assert resolved == (tmp_path / "assets" / "logo.png").resolve()

    # directories, traversal, and .git are all refused
    with pytest.raises(WorkspaceError):
        ws.file_path("assets")
    with pytest.raises(WorkspaceError):
        ws.file_path("../escape.png")
    with pytest.raises(WorkspaceError):
        ws.file_path(".git/config")


def test_safe_path_blocks_traversal(tmp_path):
    ws = _init_repo(tmp_path)
    with pytest.raises(WorkspaceError):
        ws.read_file("../escape.txt")
    with pytest.raises(WorkspaceError):
        ws.list_tree("../..")


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
