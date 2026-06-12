import base64
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
    assert r.json()["running"] is False  # no turn in flight on a fresh project
    assert "myrepo" in [p["name"] for p in client.get("/api/projects").json()]

    # a second clone of the same repo gets a suffix
    assert client.post("/api/projects", json={"remote_url": src}).json()["name"] == "myrepo_2"
    # empty url is rejected
    assert client.post("/api/projects", json={"remote_url": ""}).status_code == 400


def test_models_endpoint(tmp_path):
    client, _ = _client(tmp_path)
    assert client.get("/api/models").status_code == 401  # auth required
    client.post("/api/register", json={"username": "a", "password": "p"})

    body = client.get("/api/models").json()
    assert body["default"] == Config().model
    ids = [m["id"] for m in body["models"]]
    assert Config().model in ids
    assert all("id" in m and "label" in m for m in body["models"])


def test_project_model_endpoint(tmp_path):
    _src_repo(tmp_path / "myrepo")
    client, _ = _client(tmp_path)
    client.post("/api/register", json={"username": "a", "password": "p"})
    src = str(tmp_path / "myrepo")

    # create with an explicit model → it's exposed on the project
    proj = client.post(
        "/api/projects", json={"remote_url": src, "model": "claude-sonnet-4-6"}
    ).json()
    pid = proj["id"]
    assert proj["model"] == "claude-sonnet-4-6"

    # a project with no override reports the lab default
    other = client.post("/api/projects", json={"remote_url": src}).json()
    assert other["model"] == Config().model

    # switch the model mid-session; clearing falls back to the default
    r = client.post(f"/api/projects/{pid}/model", json={"model": "claude-haiku-4-5-20251001"})
    assert r.status_code == 200 and r.json()["model"] == "claude-haiku-4-5-20251001"
    listed = [p for p in client.get("/api/projects").json() if p["id"] == pid][0]
    assert listed["model"] == "claude-haiku-4-5-20251001"
    r = client.post(f"/api/projects/{pid}/model", json={"model": ""})
    assert r.json()["model"] == Config().model

    # an unknown model is rejected
    assert client.post(f"/api/projects/{pid}/model", json={"model": "gpt-9"}).status_code == 400


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
    assert client.post("/api/projects/9999/token", json={"github_token": "x"}).status_code == 404


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
    assert client.post("/api/projects/9999/clear").status_code == 404


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


def test_agent_config_and_skills_endpoints(tmp_path):
    _src_repo(tmp_path / "myrepo")
    client, _ = _client(tmp_path)
    client.post("/api/register", json={"username": "a", "password": "p"})
    pid = client.post("/api/projects", json={"remote_url": str(tmp_path / "myrepo")}).json()["id"]

    # defaults: empty prompt, empty mcp config, no skills
    cfg = client.get(f"/api/projects/{pid}/agent").json()
    assert cfg == {"agent_prompt": "", "mcp_servers": "", "skills": []}

    # save prompt + valid mcp servers; invalid JSON / shape are 400s
    r = client.post(f"/api/projects/{pid}/agent", json={
        "agent_prompt": "Prefer small commits.",
        "mcp_servers": '{"docs": {"type": "http", "url": "https://x/mcp"}}',
    })
    assert r.status_code == 200
    assert client.post(
        f"/api/projects/{pid}/agent", json={"mcp_servers": "not json"}
    ).status_code == 400
    assert client.post(
        f"/api/projects/{pid}/agent", json={"mcp_servers": '{"docs": "https://x"}'}
    ).status_code == 400
    cfg = client.get(f"/api/projects/{pid}/agent").json()
    assert cfg["agent_prompt"] == "Prefer small commits."
    assert "https://x/mcp" in cfg["mcp_servers"]

    # skills: an uploaded SKILL.md names itself via frontmatter, is committed,
    # listed as a row, lands in the tree, and can be removed (also committed)
    skill_md = "---\nname: review\ndescription: review hard\n---\n\nReview hard."
    r = client.post(
        f"/api/projects/{pid}/skills",
        files=[("files", ("whatever.md", skill_md.encode(), "text/markdown"))],
    )
    assert r.status_code == 200
    assert r.json()["added"] == ["review"]  # from frontmatter, not the filename
    assert r.json()["commit"]
    assert client.get(f"/api/projects/{pid}/agent").json()["skills"] == [
        {"name": "review", "description": "review hard"}
    ]
    assert client.get(
        f"/api/projects/{pid}/file", params={"path": ".claude/skills/review/SKILL.md"}
    ).json()["content"].startswith("---")
    # a file without a frontmatter name is a per-file error, not a 500
    bad = client.post(
        f"/api/projects/{pid}/skills",
        files=[("files", ("noname.md", b"just text", "text/markdown"))],
    ).json()
    assert bad["added"] == []
    assert "name" in bad["errors"]["noname.md"]
    assert client.delete(f"/api/projects/{pid}/skills/review").json()["commit"]
    assert client.get(f"/api/projects/{pid}/agent").json()["skills"] == []
    assert client.delete(f"/api/projects/{pid}/skills/review").status_code == 400


def test_strict_project_isolation(tmp_path):
    _src_repo(tmp_path / "myrepo")
    app, _conn = _app(tmp_path)
    a = TestClient(app)  # first user → super-user
    a.post("/api/register", json={"username": "a", "password": "p"})
    code_b = a.post("/api/admin/invites").json()["code"]
    code_c = a.post("/api/admin/invites").json()["code"]
    b = TestClient(app)
    b.post("/api/register", json={"username": "b", "password": "p", "invite": code_b})
    c = TestClient(app)
    c.post("/api/register", json={"username": "c", "password": "p", "invite": code_c})

    pid = b.post("/api/projects", json={"remote_url": str(tmp_path / "myrepo")}).json()["id"]

    # each user sees only their own list; supers see everything, with owners
    assert [(p["name"], p["owner"]) for p in b.get("/api/projects").json()] == [("myrepo", "b")]
    assert c.get("/api/projects").json() == []
    super_list = {p["name"]: p["owner"] for p in a.get("/api/projects").json()}
    assert super_list["myrepo"] == "b"

    # a foreign project is a 404 (existence hidden), for reads and actions alike
    assert c.get(f"/api/projects/{pid}/tree").status_code == 404
    assert c.get(f"/api/projects/{pid}/messages").status_code == 404
    assert c.post(f"/api/projects/{pid}/reset").status_code == 404
    assert c.delete(f"/api/projects/{pid}").status_code == 404
    # while the owner and the super-user work normally
    assert b.get(f"/api/projects/{pid}/tree").status_code == 200
    assert a.get(f"/api/projects/{pid}/tree").status_code == 200

    # a checkout dropped into labs/ has no owner — super-only
    _src_repo(tmp_path / "labs" / "dropped")
    assert "dropped" not in [p["name"] for p in b.get("/api/projects").json()]
    assert "dropped" in [p["name"] for p in a.get("/api/projects").json()]


def test_create_blank_project_endpoint(tmp_path):
    client, _ = _client(tmp_path)
    client.post("/api/register", json={"username": "a", "password": "p"})

    r = client.post("/api/projects", json={"name": "scratch"})
    assert r.status_code == 200
    assert r.json()["name"] == "scratch"
    pid = r.json()["id"]
    # it's a real repo: the browser sees the seed README on main
    tree = client.get(f"/api/projects/{pid}/tree").json()
    assert tree["branch"] == "main"
    assert "README.md" in [e["name"] for e in tree["entries"]]

    assert client.post("/api/projects", json={"name": "scratch"}).status_code == 400
    assert client.post("/api/projects", json={"name": "../evil"}).status_code == 400
    # a url wins over a name when both are given (clone of a bad url fails)
    assert client.post(
        "/api/projects", json={"remote_url": str(tmp_path / "nope"), "name": "x"}
    ).status_code == 400


def test_remove_project_endpoint(tmp_path):
    _src_repo(tmp_path / "myrepo")
    app, _conn = _app(tmp_path)
    client = TestClient(app)
    client.post("/api/register", json={"username": "a", "password": "p"})
    pid = client.post("/api/projects", json={"remote_url": str(tmp_path / "myrepo")}).json()["id"]
    _fake_mirror_client(app, "myrepo", {"out.txt": b"x"})

    r = client.delete(f"/api/projects/{pid}")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "myrepo"
    assert body["mirrors_cleaned"] == ["mac"]
    assert client.get("/api/projects").json() == []
    assert not (tmp_path / "labs" / "myrepo").exists()
    assert client.delete(f"/api/projects/{pid}").status_code == 404

    fresh, _ = _client(tmp_path / "fresh")
    assert fresh.delete("/api/projects/1").status_code == 401


def test_upload_endpoints(tmp_path):
    _src_repo(tmp_path / "myrepo")
    client, _ = _client(tmp_path)
    client.post("/api/register", json={"username": "a", "password": "p"})
    pid = client.post("/api/projects", json={"remote_url": str(tmp_path / "myrepo")}).json()["id"]

    # repo upload: lands in the tree (committed) and is readable back
    r = client.post(
        f"/api/projects/{pid}/upload",
        files=[("files", ("notes.txt", b"hello", "text/plain"))],
        data={"dest": "docs"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["written"] == ["docs/notes.txt"]
    assert body["commit"]
    assert client.get(
        f"/api/projects/{pid}/file", params={"path": "docs/notes.txt"}
    ).json()["content"] == "hello"

    # chat upload: saved under .lab-uploads/, path comes back for the message
    r = client.post(
        f"/api/projects/{pid}/chat-upload",
        files=[("files", ("shot.png", b"\x89PNG", "image/png"))],
    )
    assert r.status_code == 200
    saved = r.json()["saved"]
    assert len(saved) == 1
    assert saved[0]["name"] == "shot.png"
    assert saved[0]["path"].startswith(".lab-uploads/")

    # both require auth
    fresh, _ = _client(tmp_path / "fresh")
    assert fresh.post(
        "/api/projects/1/upload", files=[("files", ("a", b"x", "text/plain"))]
    ).status_code == 401
    assert fresh.post(
        "/api/projects/1/chat-upload", files=[("files", ("a", b"x", "text/plain"))]
    ).status_code == 401


def test_rebase_reset_archive_endpoints(tmp_path):
    import io
    import zipfile

    _src_repo(tmp_path / "myrepo")
    client, _ = _client(tmp_path)
    client.post("/api/register", json={"username": "a", "password": "p"})
    pid = client.post("/api/projects", json={"remote_url": str(tmp_path / "myrepo")}).json()["id"]

    # rebase needs a chat branch first
    r = client.post(f"/api/projects/{pid}/rebase")
    assert r.status_code == 400
    assert "no work branch" in r.json()["detail"]

    # reset reports the branch and commit it kept
    r = client.post(f"/api/projects/{pid}/reset")
    assert r.status_code == 200
    assert r.json()["commit"]

    # archive streams a zip of the working tree
    r = client.get(f"/api/projects/{pid}/archive")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert ".zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert "README.md" in zf.namelist()

    # all three require auth
    fresh, _ = _client(tmp_path / "fresh")
    assert fresh.post("/api/projects/1/rebase").status_code == 401
    assert fresh.post("/api/projects/1/reset").status_code == 401
    assert fresh.get("/api/projects/1/archive").status_code == 401


# --- client-mirror browsing (files dialog source tabs) -------------------------

def _fake_mirror_client(app, project_name, files):
    """Register a scripted client that answers mirror/fetch/clean instantly."""
    registry = app.state.registry

    async def send(message):
        t, tid = message["type"], message["task_id"]
        if t == "mirror":
            exists = message["project"] == project_name
            await registry.handle_message("mac", {
                "type": "mirror_result", "task_id": tid, "exists": exists,
                "manifest": {p: "h" for p in files} if exists else {},
            })
        elif t == "fetch":
            for p in message["paths"]:
                frame = {"type": "file", "task_id": tid, "path": p}
                if p in files:
                    frame["data"] = base64.b64encode(files[p]).decode()
                else:
                    frame.update(data=None, error="missing")
                await registry.handle_message("mac", frame)
            await registry.handle_message("mac", {"type": "fetch_done", "task_id": tid})
        elif t == "clean":
            await registry.handle_message("mac", {"type": "clean_done", "task_id": tid, "ok": True})

    registry.register(name="mac", platform="darwin", capabilities=[], send=send)


def test_client_mirror_browse_fetch_and_clean(tmp_path):
    _src_repo(tmp_path / "myrepo")
    app, _conn = _app(tmp_path)
    client = TestClient(app)
    client.post("/api/register", json={"username": "a", "password": "p"})
    pid = client.post("/api/projects", json={"remote_url": str(tmp_path / "myrepo")}).json()["id"]
    files = {"out/report.txt": b"all green", "img.png": b"\x89PNG\x00bytes"}
    _fake_mirror_client(app, "myrepo", files)

    # only clients holding a mirror of this project are listed
    r = client.get(f"/api/projects/{pid}/clients")
    assert r.json() == [{"name": "mac", "platform": "darwin"}]

    # flat mirror listing the UI nests into a tree
    r = client.get(f"/api/projects/{pid}/clients/mac/mirror")
    assert r.json() == {"exists": True, "paths": ["img.png", "out/report.txt"]}

    # file view: text content, binary flagged, missing reported as 400
    body = client.get(
        f"/api/projects/{pid}/clients/mac/file", params={"path": "out/report.txt"}
    ).json()
    assert body["content"] == "all green"
    assert body["binary"] is False
    assert client.get(
        f"/api/projects/{pid}/clients/mac/file", params={"path": "img.png"}
    ).json()["binary"] is True
    assert client.get(
        f"/api/projects/{pid}/clients/mac/file", params={"path": "nope"}
    ).status_code == 400

    # raw bytes for the browser, content type guessed from the name
    r = client.get(f"/api/projects/{pid}/clients/mac/raw", params={"path": "img.png"})
    assert r.content == files["img.png"]
    assert r.headers["content-type"].startswith("image/png")

    # fetch copies into the lab working tree (visible via the lab file endpoint)
    body = client.post(
        f"/api/projects/{pid}/clients/mac/fetch",
        json={"paths": ["out/report.txt", "nope"]},
    ).json()
    assert body["written"] == ["out/report.txt"]
    assert "nope" in body["errors"]
    assert client.get(
        f"/api/projects/{pid}/file", params={"path": "out/report.txt"}
    ).json()["content"] == "all green"
    # empty fetch is rejected
    assert client.post(
        f"/api/projects/{pid}/clients/mac/fetch", json={"paths": []}
    ).status_code == 400

    # clean reports ok; unknown client is a clean 400
    assert client.post(f"/api/projects/{pid}/clients/mac/clean").json() == {"ok": True}
    assert client.post(f"/api/projects/{pid}/clients/ghost/clean").status_code == 400


def test_client_mirror_routes_require_auth(tmp_path):
    client, _ = _client(tmp_path)
    assert client.get("/api/projects/1/clients").status_code == 401
    assert client.get("/api/projects/1/clients/mac/mirror").status_code == 401
    assert client.get("/api/projects/1/clients/mac/file?path=x").status_code == 401
    assert client.get("/api/projects/1/clients/mac/raw?path=x").status_code == 401
    assert client.post("/api/projects/1/clients/mac/fetch").status_code == 401
    assert client.post("/api/projects/1/clients/mac/clean").status_code == 401
