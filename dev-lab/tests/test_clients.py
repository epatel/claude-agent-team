import asyncio
import base64
import json

import pytest
from dev_lab import db
from dev_lab.clients import ClientError, ClientRegistry
from dev_lab.config import Config
from dev_lab.web import build_app
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

# --- registry unit tests -----------------------------------------------------

def _registry_with_client(sent):
    reg = ClientRegistry()

    async def send(message):
        sent.append(message)

    name = reg.register(
        name="mac", platform="darwin", capabilities=[{"name": "run_tests"}], send=send
    )
    return reg, name


def test_register_dedupes_names_and_lists():
    reg = ClientRegistry()

    async def send(_):
        pass

    assert reg.register(name="mac", platform="darwin", capabilities=[], send=send) == "mac"
    assert reg.register(name="mac", platform="darwin", capabilities=[], send=send) == "mac_2"
    assert [c["name"] for c in reg.list()] == ["mac", "mac_2"]
    reg.unregister("mac")
    assert [c["name"] for c in reg.list()] == ["mac_2"]


def test_run_dispatches_task_serves_files_and_returns_result(tmp_path):
    (tmp_path / "a.py").write_text("hello")
    sent = []
    reg, name = _registry_with_client(sent)

    async def scenario():
        run = asyncio.create_task(
            reg.run(name, project_root=tmp_path, command="make test")
        )
        await asyncio.sleep(0)  # let the task message go out
        task_msg = sent[0]
        assert task_msg["type"] == "task"
        assert task_msg["command"] == "make test"
        assert "a.py" in task_msg["manifest"]

        # client asks for the file it lacks → lab serves it
        await reg.handle_message(name, {
            "type": "need", "task_id": task_msg["task_id"], "paths": ["a.py"],
        })
        file_msg = sent[1]
        assert file_msg["type"] == "file"
        assert base64.b64decode(file_msg["data"]) == b"hello"

        # client reports the run result
        await reg.handle_message(name, {
            "type": "result", "task_id": task_msg["task_id"],
            "ok": True, "returncode": 0, "stdout": "1 passed", "stderr": "",
            "changed": {"added": ["out.log"], "modified": [], "deleted": []},
        })
        return await run

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert result["stdout"] == "1 passed"
    assert result["changed"]["added"] == ["out.log"]
    assert result["manifest_hash"]


def test_run_unknown_client_and_disconnect_fail_cleanly(tmp_path):
    sent = []
    reg, name = _registry_with_client(sent)

    with pytest.raises(ClientError, match="no connected client"):
        asyncio.run(reg.run("nope", project_root=tmp_path, command="true"))

    async def disconnect_mid_task():
        run = asyncio.create_task(reg.run(name, project_root=tmp_path, command="true"))
        await asyncio.sleep(0)
        reg.unregister(name)
        return await run

    with pytest.raises(ClientError, match="disconnected"):
        asyncio.run(disconnect_mid_task())


def test_file_requests_outside_root_report_error_not_content(tmp_path):
    (tmp_path / "a.py").write_text("x")
    sent = []
    reg, name = _registry_with_client(sent)

    async def scenario():
        run = asyncio.create_task(reg.run(name, project_root=tmp_path, command="true"))
        await asyncio.sleep(0)
        task_id = sent[0]["task_id"]
        await reg.handle_message(name, {
            "type": "need", "task_id": task_id, "paths": ["../../etc/passwd"],
        })
        await reg.handle_message(name, {
            "type": "result", "task_id": task_id,
            "ok": True, "returncode": 0, "stdout": "", "stderr": "", "changed": {},
        })
        await run

    asyncio.run(scenario())
    escape = sent[1]
    assert escape["type"] == "file"
    assert escape["data"] is None
    assert escape["error"]


def test_run_passes_preserve_patterns_to_the_task(tmp_path):
    sent = []
    reg, name = _registry_with_client(sent)

    async def scenario():
        run = asyncio.create_task(
            reg.run(name, project_root=tmp_path, command="make", preserve=["target/*"])
        )
        await asyncio.sleep(0)
        assert sent[0]["preserve"] == ["target/*"]
        await reg.handle_message(name, {
            "type": "result", "task_id": sent[0]["task_id"],
            "ok": True, "returncode": 0, "stdout": "", "stderr": "", "changed": {},
        })
        await run

    asyncio.run(scenario())


def test_fetch_collects_files_and_errors(tmp_path):
    sent = []
    reg, name = _registry_with_client(sent)

    async def scenario():
        fetch = asyncio.create_task(
            reg.fetch(name, project="proj", paths=["good.txt", "bad.txt"])
        )
        await asyncio.sleep(0)
        msg = sent[0]
        assert msg["type"] == "fetch"
        assert msg["project"] == "proj"
        fid = msg["task_id"]
        await reg.handle_message(name, {
            "type": "file", "task_id": fid, "path": "good.txt",
            "data": base64.b64encode(b"bytes").decode(),
        })
        await reg.handle_message(name, {
            "type": "file", "task_id": fid, "path": "bad.txt",
            "data": None, "error": "missing",
        })
        await reg.handle_message(name, {"type": "fetch_done", "task_id": fid})
        return await fetch

    out = asyncio.run(scenario())
    assert out["files"] == {"good.txt": b"bytes"}
    assert out["errors"] == {"bad.txt": "missing"}


def test_fetch_fails_on_disconnect(tmp_path):
    sent = []
    reg, name = _registry_with_client(sent)

    async def scenario():
        fetch = asyncio.create_task(reg.fetch(name, project="p", paths=["a"]))
        await asyncio.sleep(0)
        reg.unregister(name)
        return await fetch

    with pytest.raises(ClientError, match="disconnected"):
        asyncio.run(scenario())


def test_mirror_returns_listing_and_clean_reports_ok():
    sent = []
    reg, name = _registry_with_client(sent)

    async def scenario():
        mirror = asyncio.create_task(reg.mirror(name, project="proj"))
        await asyncio.sleep(0)
        msg = sent[0]
        assert msg["type"] == "mirror"
        assert msg["project"] == "proj"
        await reg.handle_message(name, {
            "type": "mirror_result", "task_id": msg["task_id"],
            "exists": True, "manifest": {"a.py": "h1", "out/r.txt": "h2"},
        })
        listing = await mirror

        clean = asyncio.create_task(reg.clean(name, project="proj"))
        await asyncio.sleep(0)
        msg = sent[1]
        assert msg["type"] == "clean"
        assert msg["project"] == "proj"
        await reg.handle_message(
            name, {"type": "clean_done", "task_id": msg["task_id"], "ok": True}
        )
        return listing, await clean

    listing, cleaned = asyncio.run(scenario())
    assert listing == {"exists": True, "manifest": {"a.py": "h1", "out/r.txt": "h2"}}
    assert cleaned == {"ok": True, "error": None}


def test_mirror_fails_on_disconnect():
    sent = []
    reg, name = _registry_with_client(sent)

    async def scenario():
        mirror = asyncio.create_task(reg.mirror(name, project="p"))
        await asyncio.sleep(0)
        reg.unregister(name)
        return await mirror

    with pytest.raises(ClientError, match="disconnected"):
        asyncio.run(scenario())


# --- /ws/client endpoint -----------------------------------------------------

def _client_app(tmp_path, **config_kwargs):
    conn = db.connect(tmp_path / "lab.db")
    app = build_app(
        labs_dir=tmp_path / "labs",
        config=Config(**config_kwargs),
        conn=conn,
        secret="test-secret",
    )
    return TestClient(app), app


def test_ws_client_hello_registers_and_disconnect_unregisters(tmp_path):
    client, app = _client_app(tmp_path)
    with client.websocket_connect("/ws/client") as ws:
        ws.send_text(json.dumps({
            "type": "hello", "name": "mac", "platform": "darwin",
            "capabilities": [{"name": "run_tests"}],
        }))
        ok = json.loads(ws.receive_text())
        assert ok == {"type": "hello_ok", "name": "mac"}
        listed = app.state.registry.list()
        assert listed == [{
            "name": "mac", "platform": "darwin",
            "capabilities": [{"name": "run_tests"}],
        }]
    assert app.state.registry.list() == []


def test_ws_client_rejects_bad_token_and_bad_first_frame(tmp_path):
    client, app = _client_app(tmp_path, client_token="sekret")
    with client.websocket_connect("/ws/client") as ws:
        ws.send_text(json.dumps({"type": "hello", "name": "mac", "token": "wrong"}))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert app.state.registry.list() == []

    with client.websocket_connect("/ws/client") as ws:
        ws.send_text(json.dumps({"type": "not-hello"}))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert app.state.registry.list() == []


def test_ws_client_with_correct_token_registers(tmp_path):
    client, app = _client_app(tmp_path, client_token="sekret")
    with client.websocket_connect("/ws/client") as ws:
        ws.send_text(json.dumps({"type": "hello", "name": "mac", "token": "sekret"}))
        assert json.loads(ws.receive_text())["type"] == "hello_ok"


def test_api_clients_requires_auth_and_lists(tmp_path):
    client, app = _client_app(tmp_path)
    assert client.get("/api/clients").status_code == 401
    client.post("/api/register", json={"username": "a", "password": "p"})
    assert client.get("/api/clients").json() == []
