import asyncio
import subprocess
from pathlib import Path

import pytest
from dev_lab import db
from dev_lab.agent import AgentResult
from dev_lab.config import Config
from dev_lab.projects import ProjectError, ProjectManager


def _src_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "x@y.z"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def _pm(tmp_path, run_task=None):
    conn = db.connect(tmp_path / "lab.db")
    kwargs = {"labs_dir": tmp_path / "labs", "config": Config(github_token="x"), "conn": conn}
    if run_task is not None:
        kwargs["run_task"] = run_task
    return ProjectManager(**kwargs), conn, tmp_path / "labs"


def test_create_derives_name_from_url(tmp_path):
    src = tmp_path / "myrepo"
    _src_repo(src)
    pm, _conn, labs = _pm(tmp_path)

    row = pm.create(str(src))

    assert row["name"] == "myrepo"  # derived from the repo path, not a given name
    assert (labs / "myrepo" / ".git").exists()
    assert (labs / "myrepo" / "README.md").exists()


def test_create_dedupes_with_suffix(tmp_path):
    src = tmp_path / "myrepo"
    _src_repo(src)
    pm, _conn, _labs = _pm(tmp_path)

    assert pm.create(str(src))["name"] == "myrepo"
    assert pm.create(str(src))["name"] == "myrepo_2"
    assert pm.create(str(src))["name"] == "myrepo_3"


def test_create_rejects_empty_url(tmp_path):
    pm, _conn, _labs = _pm(tmp_path)
    with pytest.raises(ProjectError, match="git URL"):
        pm.create("")


def test_discover_finds_existing_checkout(tmp_path):
    pm, _conn, labs = _pm(tmp_path)
    _src_repo(labs / "dropped")  # a checkout placed directly under labs/

    rows = pm.discover()

    assert any(r["name"] == "dropped" for r in rows)


def test_merge_to_base_lands_work(tmp_path):
    src = tmp_path / "src"
    _src_repo(src)

    async def fake(message, *, cwd, model, resume=None, on_event=None, extensions=None):
        (Path(cwd) / "feature.txt").write_text("x\n")
        return AgentResult("ok", 1, False, 0.0, session_id="s")

    pm, _conn, labs = _pm(tmp_path, run_task=fake)
    pid = pm.create(str(src))["id"]
    asyncio.run(pm.run_turn(pid, "add feature"))  # commits feature.txt on chat/<ts>

    result = asyncio.run(pm.merge_to_base(pid))

    clone = labs / "src"
    base = result["base"]
    assert result["branch"].startswith("chat/")
    got = subprocess.run(
        ["git", "-C", str(clone), "cat-file", "-e", f"{base}:feature.txt"]
    ).returncode
    assert got == 0  # the base branch now has the merged work


def test_merge_without_work_errors(tmp_path):
    src = tmp_path / "src"
    _src_repo(src)
    pm, _conn, _labs = _pm(tmp_path)
    pid = pm.create(str(src))["id"]
    with pytest.raises(ProjectError, match="no work to merge"):
        asyncio.run(pm.merge_to_base(pid))


def test_run_turn_persists_messages_and_branch(tmp_path):
    src = tmp_path / "src"
    _src_repo(src)

    async def fake(message, *, cwd, model, resume=None, on_event=None, extensions=None):
        (Path(cwd) / "f.txt").write_text("x\n")
        return AgentResult("did it", 1, False, 0.0, session_id="sess-9")

    pm, conn, _labs = _pm(tmp_path, run_task=fake)
    pid = pm.create(str(src))["id"]

    result = asyncio.run(pm.run_turn(pid, "do a thing"))

    assert result.committed
    msgs = db.list_messages(conn, pid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "did it"
    prow = db.get_project(conn, pid)
    assert prow["last_session_id"] == "sess-9"
    assert prow["branch"] == result.branch
