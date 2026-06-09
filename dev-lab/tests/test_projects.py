import asyncio
import subprocess
from pathlib import Path

import pytest
from dev_lab import db
from dev_lab.agent import AgentResult
from dev_lab.config import Config
from dev_lab.projects import ProjectError, ProjectManager, _authed_url, _strip_credentials


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
    kwargs = {"labs_dir": tmp_path / "labs", "config": Config(), "conn": conn}
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


def test_merge_base_into_branch_pulls_base_commits(tmp_path):
    src = tmp_path / "src"
    _src_repo(src)

    async def fake(message, *, cwd, model, resume=None, on_event=None, extensions=None):
        (Path(cwd) / "feature.txt").write_text("x\n")
        return AgentResult("ok", 1, False, 0.0, session_id="s")

    pm, conn, labs = _pm(tmp_path, run_task=fake)
    pid = pm.create(str(src))["id"]
    asyncio.run(pm.run_turn(pid, "add feature"))  # commits feature.txt on chat/<ts>

    clone = labs / "src"
    base = pm.effective_base(db.get_project(conn, pid))
    branch = pm.open(pid).branch
    # land a commit on the base branch that the chat branch doesn't have yet
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", base], check=True)
    (clone / "base_only.txt").write_text("b\n")
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-q", "-m", "base work"], check=True)
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", branch], check=True)

    result = asyncio.run(pm.merge_base_into_branch(pid))

    assert result["base"] == base
    assert result["branch"] == branch
    # the chat branch now carries the base-only commit
    got = subprocess.run(
        ["git", "-C", str(clone), "cat-file", "-e", f"{branch}:base_only.txt"]
    ).returncode
    assert got == 0
    # and the working tree is left back on the chat branch for the next turn
    head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == branch


def test_merge_base_without_work_errors(tmp_path):
    src = tmp_path / "src"
    _src_repo(src)
    pm, _conn, _labs = _pm(tmp_path)
    pid = pm.create(str(src))["id"]
    with pytest.raises(ProjectError, match="no work branch yet"):
        asyncio.run(pm.merge_base_into_branch(pid))


def test_set_base_branch_validates_and_persists(tmp_path):
    src = tmp_path / "src"
    _src_repo(src)
    pm, conn, labs = _pm(tmp_path)
    pid = pm.create(str(src))["id"]
    # A real extra branch in the clone to target.
    subprocess.run(["git", "-C", str(labs / "src"), "branch", "release"], check=True)

    result = asyncio.run(pm.set_base_branch(pid, "release"))

    assert result["base_branch"] == "release"
    assert db.get_project(conn, pid)["base_branch"] == "release"


def test_set_base_branch_rejects_unknown(tmp_path):
    src = tmp_path / "src"
    _src_repo(src)
    pm, _conn, _labs = _pm(tmp_path)
    pid = pm.create(str(src))["id"]
    with pytest.raises(ProjectError, match="no such branch"):
        asyncio.run(pm.set_base_branch(pid, "nope"))


def test_list_branches_reports_branches_and_base(tmp_path):
    src = tmp_path / "src"
    _src_repo(src)
    pm, _conn, labs = _pm(tmp_path)
    pid = pm.create(str(src))["id"]
    subprocess.run(["git", "-C", str(labs / "src"), "branch", "release"], check=True)

    out = asyncio.run(pm.list_branches(pid))

    assert "release" in out["branches"]
    assert out["base"] in ("main", "master")  # repo default while unset

    # once a base is configured, list_branches reports it
    asyncio.run(pm.set_base_branch(pid, "release"))
    assert asyncio.run(pm.list_branches(pid))["base"] == "release"


def test_list_branches_unknown_project_errors(tmp_path):
    pm, _conn, _labs = _pm(tmp_path)
    with pytest.raises(ProjectError, match="no such project"):
        asyncio.run(pm.list_branches(999))


def test_new_session_branches_off_configured_base(tmp_path):
    src = tmp_path / "src"
    _src_repo(src)

    async def fake(message, *, cwd, model, resume=None, on_event=None, extensions=None):
        (Path(cwd) / "feature.txt").write_text("x\n")
        return AgentResult("ok", 1, False, 0.0, session_id="s")

    pm, _conn, labs = _pm(tmp_path, run_task=fake)
    pid = pm.create(str(src))["id"]
    clone = labs / "src"
    # Give the base its own commit so it diverges from the repo default.
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "-b", "release"], check=True)
    (clone / "rel.txt").write_text("r\n")
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-q", "-m", "rel"], check=True)
    release_tip = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "release"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    asyncio.run(pm.set_base_branch(pid, "release"))
    result = asyncio.run(pm.run_turn(pid, "add feature"))

    # the chat branch's commit is parented on the configured base's tip
    parent = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", f"{result.branch}^"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert parent == release_tip


def test_merge_uses_configured_base(tmp_path):
    src = tmp_path / "src"
    _src_repo(src)

    async def fake(message, *, cwd, model, resume=None, on_event=None, extensions=None):
        (Path(cwd) / "feature.txt").write_text("x\n")
        return AgentResult("ok", 1, False, 0.0, session_id="s")

    pm, _conn, labs = _pm(tmp_path, run_task=fake)
    pid = pm.create(str(src))["id"]
    subprocess.run(["git", "-C", str(labs / "src"), "branch", "release"], check=True)
    asyncio.run(pm.set_base_branch(pid, "release"))
    asyncio.run(pm.run_turn(pid, "add feature"))

    result = asyncio.run(pm.merge_to_base(pid))

    assert result["base"] == "release"  # configured base, not the repo default
    clone = labs / "src"
    got = subprocess.run(
        ["git", "-C", str(clone), "cat-file", "-e", "release:feature.txt"]
    ).returncode
    assert got == 0


def test_authed_url_embeds_and_strips_token():
    url = "https://github.com/owner/repo.git"
    assert _authed_url(url, "tok") == "https://x-access-token:tok@github.com/owner/repo.git"
    # no token -> unchanged; ssh remote -> unchanged (token does not apply)
    assert _authed_url(url, None) == url
    assert _authed_url("git@github.com:owner/repo.git", "tok") == "git@github.com:owner/repo.git"
    # an already-tokened url is re-tokened cleanly, never double-embedded
    again = _authed_url(_authed_url(url, "old"), "new")
    assert again == "https://x-access-token:new@github.com/owner/repo.git"


def test_strip_credentials_removes_embedded_auth():
    assert (
        _strip_credentials("https://x-access-token:tok@github.com/o/r.git")
        == "https://github.com/o/r.git"
    )
    assert _strip_credentials("https://github.com/o/r.git") == "https://github.com/o/r.git"


def test_create_persists_token_and_strips_url(tmp_path):
    src = tmp_path / "myrepo"
    _src_repo(src)
    pm, conn, _labs = _pm(tmp_path)

    row = pm.create(str(src), github_token="sekret")

    assert row["github_token"] == "sekret"
    # never persist a credential-bearing remote_url
    assert "@" not in (row["remote_url"] or "")


def test_create_without_token_has_none(tmp_path):
    src = tmp_path / "myrepo"
    _src_repo(src)
    pm, conn, _labs = _pm(tmp_path)

    row = pm.create(str(src))

    assert row["github_token"] is None


def test_set_token_sets_and_clears(tmp_path):
    src = tmp_path / "src"
    _src_repo(src)
    pm, conn, _labs = _pm(tmp_path)
    pid = pm.create(str(src))["id"]

    out = asyncio.run(pm.set_token(pid, "tok"))
    assert out["has_token"] is True
    assert db.get_project(conn, pid)["github_token"] == "tok"

    out = asyncio.run(pm.set_token(pid, ""))
    assert out["has_token"] is False
    assert db.get_project(conn, pid)["github_token"] is None


def test_set_token_unknown_project_errors(tmp_path):
    pm, _conn, _labs = _pm(tmp_path)
    with pytest.raises(ProjectError, match="no such project"):
        asyncio.run(pm.set_token(999, "tok"))


def test_create_persists_valid_model_and_rejects_unknown(tmp_path):
    src = tmp_path / "src"
    _src_repo(src)
    pm, conn, _labs = _pm(tmp_path)

    row = pm.create(str(src), model="claude-sonnet-4-6")
    assert db.get_project(conn, row["id"])["model"] == "claude-sonnet-4-6"

    with pytest.raises(ProjectError, match="unknown model"):
        pm.create(str(src), model="gpt-9")


def test_effective_model_falls_back_to_lab_default(tmp_path):
    src = tmp_path / "src"
    _src_repo(src)
    pm, conn, _labs = _pm(tmp_path)  # Config() default == claude-opus-4-8
    pid = pm.create(str(src))["id"]  # no override

    assert pm.effective_model(db.get_project(conn, pid)) == Config().model


def test_set_model_validates_persists_and_takes_effect_next_turn(tmp_path):
    src = tmp_path / "src"
    _src_repo(src)

    seen = []

    async def fake(message, *, cwd, model, resume=None, on_event=None, extensions=None):
        seen.append(model)
        (Path(cwd) / "f.txt").write_text("x\n")
        return AgentResult("ok", 1, False, 0.0, session_id="s")

    pm, conn, _labs = _pm(tmp_path, run_task=fake)
    pid = pm.create(str(src))["id"]

    asyncio.run(pm.run_turn(pid, "first"))  # runs on the lab default
    out = asyncio.run(pm.set_model(pid, "claude-haiku-4-5-20251001"))
    asyncio.run(pm.run_turn(pid, "second"))  # rebuilt session uses the new model

    assert out["model"] == "claude-haiku-4-5-20251001"
    assert db.get_project(conn, pid)["model"] == "claude-haiku-4-5-20251001"
    assert seen == [Config().model, "claude-haiku-4-5-20251001"]

    # empty clears back to the lab default
    out = asyncio.run(pm.set_model(pid, ""))
    assert out["model"] == Config().model
    assert db.get_project(conn, pid)["model"] is None


def test_set_model_rejects_unknown_and_unknown_project(tmp_path):
    src = tmp_path / "src"
    _src_repo(src)
    pm, _conn, _labs = _pm(tmp_path)
    pid = pm.create(str(src))["id"]
    with pytest.raises(ProjectError, match="unknown model"):
        asyncio.run(pm.set_model(pid, "gpt-9"))
    with pytest.raises(ProjectError, match="no such project"):
        asyncio.run(pm.set_model(999, "claude-sonnet-4-6"))


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
