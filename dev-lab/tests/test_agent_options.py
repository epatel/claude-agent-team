import asyncio
import json

from dev_lab.agent import _client_tools, build_agent_options
from dev_lab.clients import ClientRegistry


def test_options_defaults():
    opts = build_agent_options(cwd="/tmp", model="m")
    assert opts.mcp_servers == {}
    assert "Bash" in opts.allowed_tools
    assert not any(t.startswith("mcp__") for t in opts.allowed_tools)


def test_options_with_client_registry_adds_lab_toolset(tmp_path):
    opts = build_agent_options(cwd=tmp_path, model="m", client_registry=ClientRegistry())
    assert opts.mcp_servers["lab"]["type"] == "sdk"
    assert "mcp__lab" in opts.allowed_tools


def test_options_with_project_agent_config():
    opts = build_agent_options(
        cwd="/tmp", model="m",
        system_append="Always write tests first.",
        extra_mcp_servers={"docs": {"type": "http", "url": "https://x/mcp"}},
    )
    assert "Always write tests first." in opts.system_prompt["append"]
    assert "## Project instructions" in opts.system_prompt["append"]
    assert opts.mcp_servers["docs"] == {"type": "http", "url": "https://x/mcp"}
    assert "mcp__docs" in opts.allowed_tools
    assert opts.skills == "all"  # project .claude/skills/ are usable
    # pinned to project scope: the lab host's ~/.claude must not leak in
    assert opts.setting_sources == ["project"]


def _handlers(registry, root):
    return {t.name: t.handler for t in _client_tools(registry, root)}


def test_list_clients_tool_returns_registry_contents(tmp_path):
    registry = ClientRegistry()

    async def send(_):
        pass

    registry.register(
        name="mac", platform="darwin", capabilities=[{"name": "run"}], send=send
    )
    out = asyncio.run(_handlers(registry, tmp_path)["list_clients"]({}))
    listed = json.loads(out["content"][0]["text"])
    assert listed[0]["name"] == "mac"
    assert listed[0]["platform"] == "darwin"


def test_run_on_client_tool_reports_unknown_client_as_error(tmp_path):
    handlers = _handlers(ClientRegistry(), tmp_path)
    out = asyncio.run(handlers["run_on_client"]({"client": "nope", "command": "true"}))
    assert "error" in out["content"][0]["text"]
    assert out["is_error"] is True


def test_fetch_from_client_tool_writes_into_project_tree(tmp_path):
    registry = ClientRegistry()
    sent = []

    async def send(message):
        sent.append(message)

    name = registry.register(name="mac", platform="darwin", capabilities=[], send=send)
    handlers = _handlers(registry, tmp_path)

    async def scenario():
        call = asyncio.create_task(
            handlers["fetch_from_client"]({"client": name, "paths": ["bin/hello"]})
        )
        await asyncio.sleep(0)
        fid = sent[0]["task_id"]
        import base64
        await registry.handle_message(name, {
            "type": "file", "task_id": fid, "path": "bin/hello",
            "data": base64.b64encode(b"ELF...").decode(),
        })
        await registry.handle_message(name, {"type": "fetch_done", "task_id": fid})
        return await call

    out = asyncio.run(scenario())
    summary = json.loads(out["content"][0]["text"])
    assert summary["written"] == ["bin/hello"]
    assert summary["errors"] == {}
    assert (tmp_path / "bin" / "hello").read_bytes() == b"ELF..."


def test_run_on_client_tool_returns_result_json(tmp_path):
    (tmp_path / "a.py").write_text("x")
    registry = ClientRegistry()
    sent = []

    async def send(message):
        sent.append(message)

    name = registry.register(name="mac", platform="darwin", capabilities=[], send=send)
    handlers = _handlers(registry, tmp_path)

    async def scenario():
        call = asyncio.create_task(
            handlers["run_on_client"]({"client": name, "command": "make test"})
        )
        await asyncio.sleep(0)
        task_id = sent[0]["task_id"]
        await registry.handle_message(name, {
            "type": "result", "task_id": task_id,
            "ok": True, "returncode": 0, "stdout": "passed", "stderr": "",
            "changed": {"added": [], "modified": [], "deleted": []},
        })
        return await call

    out = asyncio.run(scenario())
    result = json.loads(out["content"][0]["text"])
    assert result["ok"] is True
    assert result["stdout"] == "passed"
    assert result["manifest_hash"]
