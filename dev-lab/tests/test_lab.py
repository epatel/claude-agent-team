import asyncio
import subprocess
from pathlib import Path

import pytest
from dev_lab.agent import AgentResult
from dev_lab.config import Config
from dev_lab.lab import run_once
from dev_lab.workspace import Workspace


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "lab@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Lab"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def _ok_result() -> AgentResult:
    return AgentResult(summary="done", num_turns=1, is_error=False, total_cost_usd=0.0)


def test_run_once_makes_one_commit(tmp_path):
    _init_repo(tmp_path)

    async def fake_run_task(instruction, *, cwd, model):
        (Path(cwd) / "feature.txt").write_text("added by agent\n")
        return _ok_result()

    cfg = Config(github_token="x")
    result = asyncio.run(
        run_once("add a feature file", repo_path=tmp_path, config=cfg, run_task=fake_run_task)
    )

    assert result.committed
    assert result.commit_sha and result.commit_sha != result.base_sha
    assert result.branch.startswith("lab/")

    ws = Workspace(tmp_path)
    assert ws.current_branch() == result.branch
    assert not ws.is_dirty()
    assert (tmp_path / "feature.txt").exists()


def test_run_once_no_changes_no_commit(tmp_path):
    _init_repo(tmp_path)

    async def noop_run_task(instruction, *, cwd, model):
        return _ok_result()

    cfg = Config(github_token="x")
    result = asyncio.run(
        run_once("do nothing", repo_path=tmp_path, config=cfg, run_task=noop_run_task)
    )

    assert not result.committed
    assert result.commit_sha is None


def test_run_once_rejects_dirty_tree(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("uncommitted\n")

    async def fake(instruction, *, cwd, model):
        return _ok_result()

    cfg = Config(github_token="x")
    with pytest.raises(RuntimeError, match="uncommitted"):
        asyncio.run(run_once("x", repo_path=tmp_path, config=cfg, run_task=fake))
