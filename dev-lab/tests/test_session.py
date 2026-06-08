import asyncio
import subprocess
from pathlib import Path

import pytest
from dev_lab.agent import AgentResult
from dev_lab.config import Config
from dev_lab.session import LabSession


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "lab@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Lab"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def test_first_turn_creates_branch_and_commits(tmp_path):
    _init_repo(tmp_path)

    async def fake(message, *, cwd, model, resume=None, on_event=None, extensions=None):
        (Path(cwd) / "a.txt").write_text("one\n")
        return AgentResult("did one", 1, False, 0.0, session_id="sess-1")

    session = LabSession(repo_path=tmp_path, config=Config(github_token="x"), run_task=fake)
    turn = asyncio.run(session.run_turn("add a.txt"))

    assert turn.committed
    assert turn.branch.startswith("chat/")
    assert session.session_id == "sess-1"
    assert (tmp_path / "a.txt").exists()


def test_followup_reuses_branch_and_resumes(tmp_path):
    _init_repo(tmp_path)
    resumes: list[str | None] = []

    async def fake(message, *, cwd, model, resume=None, on_event=None, extensions=None):
        resumes.append(resume)
        (Path(cwd) / f"{message}.txt").write_text("x\n")
        return AgentResult("ok", 1, False, 0.0, session_id="sess-1")

    session = LabSession(repo_path=tmp_path, config=Config(github_token="x"), run_task=fake)
    first = asyncio.run(session.run_turn("one"))
    second = asyncio.run(session.run_turn("two"))

    assert first.branch == second.branch                 # same branch across turns
    assert resumes == [None, "sess-1"]                   # first fresh, second resumes
    assert (tmp_path / "one.txt").exists()
    assert (tmp_path / "two.txt").exists()


def test_first_turn_branches_off_configured_base(tmp_path):
    _init_repo(tmp_path)
    # A second branch carrying a file the default branch doesn't have.
    subprocess.run(["git", "checkout", "-q", "-b", "release"], cwd=tmp_path, check=True)
    (tmp_path / "only-on-release.txt").write_text("r\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "release-only"], cwd=tmp_path, check=True)
    # Park HEAD somewhere that is NOT the configured base.
    subprocess.run(["git", "checkout", "-q", "main"], cwd=tmp_path, check=False)
    subprocess.run(["git", "checkout", "-q", "master"], cwd=tmp_path, check=False)

    async def fake(message, *, cwd, model, resume=None, on_event=None, extensions=None):
        (Path(cwd) / "a.txt").write_text("one\n")
        return AgentResult("did one", 1, False, 0.0, session_id="sess-1")

    session = LabSession(
        repo_path=tmp_path, config=Config(github_token="x"), base_branch="release", run_task=fake
    )
    asyncio.run(session.run_turn("add a.txt"))

    # The chat branch was cut from ``release``, so the release-only file is present.
    assert (tmp_path / "only-on-release.txt").exists()


def test_no_changes_no_commit(tmp_path):
    _init_repo(tmp_path)

    async def fake(message, *, cwd, model, resume=None, on_event=None, extensions=None):
        return AgentResult("nothing to do", 1, False, 0.0, session_id="sess-1")

    session = LabSession(repo_path=tmp_path, config=Config(github_token="x"), run_task=fake)
    turn = asyncio.run(session.run_turn("noop"))

    assert not turn.committed
    assert turn.commit_sha is None


def test_dirty_tree_rejected(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("uncommitted\n")

    async def fake(message, *, cwd, model, resume=None, on_event=None, extensions=None):
        return AgentResult("ok", 1, False, 0.0)

    session = LabSession(repo_path=tmp_path, config=Config(github_token="x"), run_task=fake)
    with pytest.raises(RuntimeError, match="uncommitted changes"):
        asyncio.run(session.run_turn("x"))
