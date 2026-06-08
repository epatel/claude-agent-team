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


def test_create_clones_and_registers(tmp_path):
    src = tmp_path / "src"
    _src_repo(src)
    pm, _conn, labs = _pm(tmp_path)

    row = pm.create("myproj", str(src))

    assert row["name"] == "myproj"
    assert (labs / "myproj" / ".git").exists()
    assert (labs / "myproj" / "README.md").exists()


def test_create_rejects_bad_name_and_dupes(tmp_path):
    src = tmp_path / "src"
    _src_repo(src)
    pm, _conn, _labs = _pm(tmp_path)

    with pytest.raises(ProjectError):
        pm.create("bad/name", str(src))

    pm.create("ok", str(src))
    with pytest.raises(ProjectError):
        pm.create("ok", str(src))


def test_discover_finds_existing_checkout(tmp_path):
    pm, _conn, labs = _pm(tmp_path)
    _src_repo(labs / "dropped")  # a checkout placed directly under labs/

    rows = pm.discover()

    assert any(r["name"] == "dropped" for r in rows)


def test_run_turn_persists_messages_and_branch(tmp_path):
    src = tmp_path / "src"
    _src_repo(src)

    async def fake(message, *, cwd, model, resume=None, on_event=None, extensions=None):
        (Path(cwd) / "f.txt").write_text("x\n")
        return AgentResult("did it", 1, False, 0.0, session_id="sess-9")

    pm, conn, _labs = _pm(tmp_path, run_task=fake)
    pid = pm.create("p", str(src))["id"]

    result = asyncio.run(pm.run_turn(pid, "do a thing"))

    assert result.committed
    msgs = db.list_messages(conn, pid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "did it"
    prow = db.get_project(conn, pid)
    assert prow["last_session_id"] == "sess-9"
    assert prow["branch"] == result.branch
