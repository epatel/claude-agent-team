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
