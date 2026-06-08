from dev_lab import db


def test_migrate_sets_user_version(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == max(v for v, _ in db.MIGRATIONS)


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "lab.db"
    db.connect(path).close()
    conn = db.connect(path)  # second connect re-runs migrate(); must not error
    assert conn.execute("PRAGMA user_version").fetchone()[0] == max(v for v, _ in db.MIGRATIONS)


def test_projects_and_messages(tmp_path):
    conn = db.connect(tmp_path / "lab.db")

    pid = db.create_project(conn, name="proj", path="/labs/proj", remote_url="https://x")
    assert db.get_project(conn, pid)["name"] == "proj"
    assert db.get_project_by_name(conn, "proj")["id"] == pid

    db.update_project(conn, pid, branch="chat/1", last_session_id="sess-1")
    row = db.get_project(conn, pid)
    assert row["branch"] == "chat/1"
    assert row["last_session_id"] == "sess-1"

    db.record_message(conn, project_id=pid, role="user", content="hi")
    db.record_message(conn, project_id=pid, role="assistant", content="hello")
    msgs = db.list_messages(conn, pid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert [p["name"] for p in db.list_projects(conn)] == ["proj"]


def test_record_and_read_run(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    row_id = db.record_run(
        conn,
        job_id="j1",
        instruction="add a file",
        repo="/r",
        status="done",
        branch="lab/x",
        base_sha="a" * 40,
        commit_sha="b" * 40,
        committed=True,
        cost_usd=0.12,
    )
    assert row_id == 1

    row = conn.execute("SELECT * FROM runs WHERE id = ?", (row_id,)).fetchone()
    assert row["instruction"] == "add a file"
    assert row["status"] == "done"
    assert row["committed"] == 1
    assert row["commit_sha"] == "b" * 40
    assert row["cost_usd"] == 0.12
