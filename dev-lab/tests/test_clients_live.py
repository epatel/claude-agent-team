"""End-to-end: a real ClientRuntime over a real websocket against a real server.

Covers the full reversed-connection path — hello/registration, task dispatch,
manifest sync (delta fetch + stray deletion), command execution in the mirror,
and the result + changed-files report — with nothing faked.
"""

import asyncio
import threading
import time

import pytest
import uvicorn
from dev_lab import db
from dev_lab.config import Config
from dev_lab.web import build_app
from platform_client.runtime import ClientRuntime, connect_once


@pytest.fixture
def served_app(tmp_path):
    conn = db.connect(tmp_path / "lab.db")
    app = build_app(
        labs_dir=tmp_path / "labs", config=Config(), conn=conn, secret="s"
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    )
    loop = asyncio.new_event_loop()
    thread = threading.Thread(
        target=loop.run_until_complete, args=(server.serve(),), daemon=True
    )
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        assert time.time() < deadline, "server did not start"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield app, loop, port
    server.should_exit = True
    thread.join(timeout=10)
    loop.close()


def test_full_round_trip_over_real_websocket(served_app, tmp_path):
    app, server_loop, port = served_app

    # the lab-side project tree to sync (one fresh file, one that will change)
    src = tmp_path / "project"
    src.mkdir()
    (src / "a.txt").write_text("alpha")
    (src / "sub").mkdir()
    (src / "sub" / "b.txt").write_text("beta")

    runtime = ClientRuntime(
        name="smoke",
        capabilities=[{"name": "run"}],
        mirrors_root=tmp_path / "mirrors",
        platform="testos",
    )
    client_thread = threading.Thread(
        target=lambda: asyncio.run(
            connect_once(f"ws://127.0.0.1:{port}/ws/client", runtime)
        ),
        daemon=True,
    )
    client_thread.start()

    registry = app.state.registry
    deadline = time.time() + 10
    while not registry.list():
        assert time.time() < deadline, "client never registered"
        time.sleep(0.02)
    assert registry.list()[0] == {
        "name": "smoke", "platform": "testos", "capabilities": [{"name": "run"}],
    }

    # dispatch a real task from the server's loop, as the agent tool would
    result = asyncio.run_coroutine_threadsafe(
        registry.run("smoke", project_root=src, command="cat a.txt sub/b.txt > out.txt"),
        server_loop,
    ).result(timeout=30)

    assert result["ok"] is True
    assert result["changed"]["added"] == ["out.txt"]
    assert result["manifest_hash"]
    mirror = tmp_path / "mirrors" / "project"
    assert (mirror / "out.txt").read_text() == "alphabeta"

    # second run: warm mirror — only the edited file syncs, old artifact is gone
    (src / "a.txt").write_text("ALPHA2")
    result2 = asyncio.run_coroutine_threadsafe(
        registry.run("smoke", project_root=src, command="cat a.txt > out2.txt"),
        server_loop,
    ).result(timeout=30)
    assert result2["ok"] is True
    assert (mirror / "a.txt").read_text() == "ALPHA2"
    assert (mirror / "out2.txt").read_text() == "ALPHA2"
    assert not (mirror / "out.txt").exists()  # prior run's artifact cleaned up

    # third run with preserve: out2.txt survives the sync this time
    result3 = asyncio.run_coroutine_threadsafe(
        registry.run("smoke", project_root=src, command="test -f out2.txt",
                     preserve=["out2.txt"]),
        server_loop,
    ).result(timeout=30)
    assert result3["ok"] is True
    assert (mirror / "out2.txt").exists()

    # and the artifact can be fetched back to the lab
    fetched = asyncio.run_coroutine_threadsafe(
        registry.fetch("smoke", project="project", paths=["out2.txt", "nope.txt"]),
        server_loop,
    ).result(timeout=30)
    assert fetched["files"]["out2.txt"] == b"ALPHA2"
    assert "nope.txt" in fetched["errors"]
