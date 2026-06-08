import subprocess

import pytest
from dev_lab import db
from dev_lab.config import Config
from dev_lab.web import build_app
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def _client(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    app = build_app(
        labs_dir=tmp_path / "labs", config=Config(github_token="x"), conn=conn, secret="test-secret"
    )
    return TestClient(app), conn


def _src_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "x@y.z"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def test_requires_auth(tmp_path):
    client, _ = _client(tmp_path)
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/me").status_code == 401


def test_register_login_logout(tmp_path):
    client, _ = _client(tmp_path)

    r = client.post("/api/register", json={"username": "alice", "password": "pw"})
    assert r.status_code == 200 and r.json()["username"] == "alice"
    assert client.get("/api/me").json()["username"] == "alice"

    # duplicate username
    dup = client.post("/api/register", json={"username": "alice", "password": "x"})
    assert dup.status_code == 409

    client.post("/api/logout")
    assert client.get("/api/me").status_code == 401

    ok = client.post("/api/login", json={"username": "alice", "password": "pw"})
    assert ok.status_code == 200
    bad = client.post("/api/login", json={"username": "alice", "password": "no"})
    assert bad.status_code == 401


def test_create_and_list_projects(tmp_path):
    _src_repo(tmp_path / "myrepo")
    client, _ = _client(tmp_path)
    client.post("/api/register", json={"username": "a", "password": "p"})

    src = str(tmp_path / "myrepo")
    r = client.post("/api/projects", json={"remote_url": src})
    assert r.status_code == 200 and r.json()["name"] == "myrepo"  # derived from the url
    assert "myrepo" in [p["name"] for p in client.get("/api/projects").json()]

    # a second clone of the same repo gets a suffix
    assert client.post("/api/projects", json={"remote_url": src}).json()["name"] == "myrepo_2"
    # empty url is rejected
    assert client.post("/api/projects", json={"remote_url": ""}).status_code == 400


def test_branches_and_set_base(tmp_path):
    _src_repo(tmp_path / "myrepo")
    # add a second branch in the source so the clone can see it
    subprocess.run(["git", "branch", "feature"], cwd=tmp_path / "myrepo", check=True)
    client, _ = _client(tmp_path)
    client.post("/api/register", json={"username": "a", "password": "p"})

    proj = client.post("/api/projects", json={"remote_url": str(tmp_path / "myrepo")}).json()
    pid = proj["id"]

    # the project dict exposes the effective base
    assert proj["base_branch"] in ("main", "master")

    # branches lists what the clone can see, plus the current effective base
    branches = client.get(f"/api/projects/{pid}/branches").json()
    assert "feature" in branches["branches"]
    assert branches["base"] == proj["base_branch"]

    # setting the base to an existing branch sticks
    r = client.post(f"/api/projects/{pid}/base", json={"branch": "feature"})
    assert r.status_code == 200 and r.json()["base_branch"] == "feature"
    assert client.get(f"/api/projects/{pid}/branches").json()["base"] == "feature"
    assert [p for p in client.get("/api/projects").json() if p["id"] == pid][0][
        "base_branch"
    ] == "feature"

    # an unknown branch is a 4xx with a message
    bad = client.post(f"/api/projects/{pid}/base", json={"branch": "nope"})
    assert bad.status_code == 400 and "nope" in bad.json()["detail"]


def test_messages_endpoint(tmp_path):
    client, conn = _client(tmp_path)
    client.post("/api/register", json={"username": "a", "password": "p"})
    pid = db.create_project(conn, name="p", path="/x")
    db.record_message(conn, project_id=pid, role="user", content="hi")
    db.record_message(conn, project_id=pid, role="assistant", content="yo")

    msgs = client.get(f"/api/projects/{pid}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_ws_requires_auth(tmp_path):
    client, _ = _client(tmp_path)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass
