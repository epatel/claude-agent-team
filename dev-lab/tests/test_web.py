import subprocess

import pytest
from dev_lab import db
from dev_lab.config import Config
from dev_lab.web import build_app
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def _app(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    app = build_app(
        labs_dir=tmp_path / "labs", config=Config(), conn=conn, secret="test-secret"
    )
    return app, conn


def _client(tmp_path):
    app, conn = _app(tmp_path)
    return TestClient(app), conn


def _register(client, username, password, invite=""):
    return client.post(
        "/api/register",
        json={"username": username, "password": password, "invite": invite},
    )


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

    # first user becomes the super-user, no invite needed
    r = _register(client, "alice", "pw")
    assert r.status_code == 200 and r.json()["username"] == "alice"
    assert r.json()["is_super"] is True
    me = client.get("/api/me").json()
    assert me["username"] == "alice" and me["is_super"] is True

    client.post("/api/logout")
    assert client.get("/api/me").status_code == 401

    ok = client.post("/api/login", json={"username": "alice", "password": "pw"})
    assert ok.status_code == 200 and ok.json()["is_super"] is True
    bad = client.post("/api/login", json={"username": "alice", "password": "no"})
    assert bad.status_code == 401


def test_auth_state_flags_first_user(tmp_path):
    client, _ = _client(tmp_path)

    # no users yet → the first registrant needs no invite
    assert client.get("/api/auth/state").json()["needs_invite"] is False

    _register(client, "alice", "pw")

    # once a user exists, registration requires an invite
    assert client.get("/api/auth/state").json()["needs_invite"] is True


def test_second_user_needs_invite(tmp_path):
    app, _ = _app(tmp_path)
    super_c = TestClient(app)
    assert _register(super_c, "root", "pw").json()["is_super"] is True

    # a fresh client registering without an invite is rejected
    bob = TestClient(app)
    assert _register(bob, "bob", "pw").status_code == 403
    # ...and with a bogus code, too
    assert _register(bob, "bob", "pw", invite="nope").status_code == 403

    # the super-user mints an invite
    code = super_c.post("/api/admin/invites").json()["code"]

    # which bob can redeem exactly once; he is NOT a super-user
    ok = _register(bob, "bob", "pw", invite=code)
    assert ok.status_code == 200 and ok.json()["is_super"] is False

    # the code is now spent — a second person can't reuse it
    carol = TestClient(app)
    assert _register(carol, "carol", "pw", invite=code).status_code == 403


def test_admin_requires_super(tmp_path):
    app, _ = _app(tmp_path)
    super_c = TestClient(app)
    _register(super_c, "root", "pw")
    code = super_c.post("/api/admin/invites").json()["code"]

    bob = TestClient(app)
    _register(bob, "bob", "pw", invite=code)

    # a normal user is locked out of every admin route
    assert bob.get("/api/admin/users").status_code == 403
    assert bob.get("/api/admin/invites").status_code == 403
    assert bob.post("/api/admin/invites").status_code == 403

    # the super-user sees both accounts
    users = super_c.get("/api/admin/users").json()
    assert {u["username"] for u in users} == {"root", "bob"}
    assert [u for u in users if u["username"] == "root"][0]["is_super"] is True


def test_block_user_blocks_login(tmp_path):
    app, _ = _app(tmp_path)
    super_c = TestClient(app)
    _register(super_c, "root", "pw")
    code = super_c.post("/api/admin/invites").json()["code"]
    bob = TestClient(app)
    _register(bob, "bob", "pw", invite=code)
    bob_id = [u for u in super_c.get("/api/admin/users").json() if u["username"] == "bob"][0]["id"]

    # block bob → his existing session is rejected and he can't log back in
    blocked = super_c.post(f"/api/admin/users/{bob_id}/block", json={"blocked": True})
    assert blocked.status_code == 200
    assert bob.get("/api/me").status_code == 403
    fresh = TestClient(app)
    assert fresh.post("/api/login", json={"username": "bob", "password": "pw"}).status_code == 403

    # unblock → login works again
    super_c.post(f"/api/admin/users/{bob_id}/block", json={"blocked": False})
    assert fresh.post("/api/login", json={"username": "bob", "password": "pw"}).status_code == 200


def test_reset_password_and_delete(tmp_path):
    app, _ = _app(tmp_path)
    super_c = TestClient(app)
    _register(super_c, "root", "pw")
    code = super_c.post("/api/admin/invites").json()["code"]
    bob = TestClient(app)
    _register(bob, "bob", "pw", invite=code)
    bob_id = [u for u in super_c.get("/api/admin/users").json() if u["username"] == "bob"][0]["id"]

    # reset bob's password — old one stops working, new one works
    reset = super_c.post(f"/api/admin/users/{bob_id}/password", json={"password": "new"})
    assert reset.status_code == 200
    fresh = TestClient(app)
    assert fresh.post("/api/login", json={"username": "bob", "password": "pw"}).status_code == 401
    assert fresh.post("/api/login", json={"username": "bob", "password": "new"}).status_code == 200

    # delete bob — he's gone
    assert super_c.delete(f"/api/admin/users/{bob_id}").status_code == 200
    assert {u["username"] for u in super_c.get("/api/admin/users").json()} == {"root"}


def test_super_cannot_lock_out_self(tmp_path):
    app, _ = _app(tmp_path)
    super_c = TestClient(app)
    _register(super_c, "root", "pw")
    root_id = super_c.get("/api/admin/users").json()[0]["id"]

    self_block = super_c.post(f"/api/admin/users/{root_id}/block", json={"blocked": True})
    assert self_block.status_code == 400
    assert super_c.delete(f"/api/admin/users/{root_id}").status_code == 400


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


def test_project_token_endpoint(tmp_path):
    _src_repo(tmp_path / "myrepo")
    client, _ = _client(tmp_path)
    client.post("/api/register", json={"username": "a", "password": "p"})
    src = str(tmp_path / "myrepo")

    # create with a token → has_token is exposed, the token itself never is
    proj = client.post("/api/projects", json={"remote_url": src, "github_token": "tok"}).json()
    pid = proj["id"]
    assert proj["has_token"] is True
    assert "github_token" not in proj
    listed = [p for p in client.get("/api/projects").json() if p["id"] == pid][0]
    assert listed["has_token"] is True

    # clearing the token flips has_token off
    r = client.post(f"/api/projects/{pid}/token", json={"github_token": ""})
    assert r.status_code == 200 and r.json()["has_token"] is False
    listed = [p for p in client.get("/api/projects").json() if p["id"] == pid][0]
    assert listed["has_token"] is False

    # setting it again flips it back on
    r = client.post(f"/api/projects/{pid}/token", json={"github_token": "tok2"})
    assert r.status_code == 200 and r.json()["has_token"] is True

    # unknown project is a clean 4xx; the route requires auth
    assert client.post("/api/projects/9999/token", json={"github_token": "x"}).status_code == 400


def test_token_endpoint_requires_auth(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/api/projects/1/token", json={"github_token": "x"}).status_code == 401


def test_create_project_without_token(tmp_path):
    _src_repo(tmp_path / "myrepo")
    client, _ = _client(tmp_path)
    client.post("/api/register", json={"username": "a", "password": "p"})

    # a public repo clones with no token; has_token is False
    proj = client.post("/api/projects", json={"remote_url": str(tmp_path / "myrepo")}).json()
    assert proj["has_token"] is False


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


def test_clear_chat_endpoint(tmp_path):
    client, conn = _client(tmp_path)
    client.post("/api/register", json={"username": "a", "password": "p"})
    pid = db.create_project(conn, name="p", path="/x")
    db.record_message(conn, project_id=pid, role="user", content="hi")
    db.record_message(conn, project_id=pid, role="assistant", content="yo")
    db.update_project(conn, pid, last_session_id="sess-123")

    r = client.post(f"/api/projects/{pid}/clear")
    assert r.status_code == 200 and r.json()["cleared"] is True

    # conversation is wiped and the resumed session is forgotten
    assert client.get(f"/api/projects/{pid}/messages").json() == []
    assert db.get_project(conn, pid)["last_session_id"] is None

    # unknown project is a clean 4xx, not a 500
    assert client.post("/api/projects/9999/clear").status_code == 400


def test_clear_chat_requires_auth(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/api/projects/1/clear").status_code == 401


def test_commit_diff_endpoint(tmp_path):
    _src_repo(tmp_path / "myrepo")
    client, _ = _client(tmp_path)
    client.post("/api/register", json={"username": "a", "password": "p"})
    proj = client.post("/api/projects", json={"remote_url": str(tmp_path / "myrepo")}).json()
    pid = proj["id"]

    # add a commit in the clone so there's a diff to show
    clone = tmp_path / "labs" / "myrepo"
    (clone / "hello.txt").write_text("hello world\n")
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add hello"], cwd=clone, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True, check=True
    ).stdout.strip()

    r = client.get(f"/api/projects/{pid}/commits/{sha}/diff")
    assert r.status_code == 200
    body = r.json()
    assert body["subject"] == "add hello"
    assert "hello.txt" in body["diff"] and "+hello world" in body["diff"]

    # a bogus sha is a clean 4xx, not a 500
    assert client.get(f"/api/projects/{pid}/commits/zzz/diff").status_code == 400


def test_tree_and_file_endpoints(tmp_path):
    _src_repo(tmp_path / "myrepo")
    client, _ = _client(tmp_path)
    client.post("/api/register", json={"username": "a", "password": "p"})
    proj = client.post("/api/projects", json={"remote_url": str(tmp_path / "myrepo")}).json()
    pid = proj["id"]

    tree = client.get(f"/api/projects/{pid}/tree").json()
    names = [e["name"] for e in tree["entries"]]
    assert "README.md" in names
    assert ".git" not in names  # .git is hidden from the browser

    # A fresh clone sits on its base branch with nothing diverged.
    assert tree["branch"] == tree["base"]
    assert tree["missing"] == 0
    assert all(e["status"] is None for e in tree["entries"])

    content = client.get(f"/api/projects/{pid}/file?path=README.md").json()
    assert content["binary"] is False and content["content"] == "seed\n"

    # An untracked file in the checkout is listed and flagged new vs base.
    (tmp_path / "labs" / proj["name"] / "scratch.txt").write_text("temp\n")
    tree2 = client.get(f"/api/projects/{pid}/tree").json()
    scratch = next(e for e in tree2["entries"] if e["name"] == "scratch.txt")
    assert scratch["status"] == "new"

    # path traversal is refused
    assert client.get(f"/api/projects/{pid}/file?path=../../etc/passwd").status_code == 400


def test_raw_endpoint_serves_bytes(tmp_path):
    _src_repo(tmp_path / "myrepo")
    client, _ = _client(tmp_path)
    client.post("/api/register", json={"username": "a", "password": "p"})
    proj = client.post("/api/projects", json={"remote_url": str(tmp_path / "myrepo")}).json()
    pid = proj["id"]

    # drop a tiny PNG into the checkout and fetch its raw bytes
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    (tmp_path / "labs" / proj["name"] / "logo.png").write_bytes(png)
    r = client.get(f"/api/projects/{pid}/raw?path=logo.png")
    assert r.status_code == 200
    assert r.content == png
    assert r.headers["content-type"].startswith("image/png")

    # traversal and .git are refused
    assert client.get(f"/api/projects/{pid}/raw?path=../../etc/passwd").status_code == 400
    assert client.get(f"/api/projects/{pid}/raw?path=.git/config").status_code == 400


def test_diff_routes_require_auth(tmp_path):
    client, _ = _client(tmp_path)
    assert client.get("/api/projects/1/commits/abc1234/diff").status_code == 401
    assert client.get("/api/projects/1/tree").status_code == 401
    assert client.get("/api/projects/1/file?path=x").status_code == 401
    assert client.get("/api/projects/1/raw?path=x").status_code == 401


def test_ws_requires_auth(tmp_path):
    client, _ = _client(tmp_path)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass
