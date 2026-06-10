import asyncio
import base64
import hashlib

import pytest
from platform_client import manifest
from platform_client.runtime import ClientRuntime, ConnectionClosed, ProtocolError


class FakeConn:
    """Scripted lab side: a queue of incoming frames, a log of outgoing ones."""

    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []
        # frames the test appends in response to what the runtime sends
        self.replies = {}

    async def send(self, message):
        self.sent.append(message)
        reply = self.replies.get(message.get("type"))
        if reply:
            self.incoming.extend(reply(message))

    async def receive(self):
        if not self.incoming:
            raise ConnectionClosed
        return self.incoming.pop(0)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _runtime(tmp_path, **kwargs):
    return ClientRuntime(
        name="mac",
        capabilities=[{"name": "run"}],
        mirrors_root=tmp_path / "mirrors",
        platform="darwin",
        **kwargs,
    )


def test_hello_announces_identity_and_token(tmp_path):
    conn = FakeConn([{"type": "hello_ok", "name": "mac_2"}])
    runtime = _runtime(tmp_path, token="sekret")
    asyncio.run(runtime.handle(conn))
    hello = conn.sent[0]
    assert hello["type"] == "hello"
    assert hello["platform"] == "darwin"
    assert hello["capabilities"] == [{"name": "run"}]
    assert hello["token"] == "sekret"
    assert runtime.assigned_name == "mac_2"


def test_rejected_hello_raises(tmp_path):
    conn = FakeConn([{"type": "error"}])
    with pytest.raises(ProtocolError):
        asyncio.run(_runtime(tmp_path).handle(conn))


def test_task_syncs_runs_and_reports_changes(tmp_path):
    files = {"a.txt": "alpha", "sub/b.txt": "beta"}
    # real hashes, as the lab would send — post-sync the mirror equals this
    source = {p: hashlib.sha256(c.encode()).hexdigest() for p, c in files.items()}
    task = {
        "type": "task",
        "task_id": "t1",
        "project": "proj",
        "command": "cat a.txt sub/b.txt > out.txt",
        "manifest": source,
    }
    conn = FakeConn([{"type": "hello_ok", "name": "mac"}, task])
    conn.replies["need"] = lambda msg: [
        {"type": "file", "task_id": "t1", "path": p, "data": _b64(files[p])}
        for p in msg["paths"]
    ]

    runtime = _runtime(tmp_path)
    asyncio.run(runtime.handle(conn))

    mirror = tmp_path / "mirrors" / "proj"
    assert (mirror / "a.txt").read_text() == "alpha"
    assert (mirror / "out.txt").read_text() == "alphabeta"
    result = conn.sent[-1]
    assert result["type"] == "result"
    assert result["ok"] is True
    assert result["changed"]["added"] == ["out.txt"]
    assert result["changed"]["modified"] == []


def test_warm_mirror_only_fetches_delta_and_deletes_strays(tmp_path):
    mirror = tmp_path / "mirrors" / "proj"
    mirror.mkdir(parents=True)
    (mirror / "keep.txt").write_text("same")
    (mirror / "stale.txt").write_text("old artifact")
    keep_hash = manifest.scan(mirror)["keep.txt"]

    task = {
        "type": "task",
        "task_id": "t1",
        "project": "proj",
        "command": "true",
        "manifest": {"keep.txt": keep_hash},  # stale.txt absent → deleted
    }
    conn = FakeConn([{"type": "hello_ok", "name": "mac"}, task])
    asyncio.run(_runtime(tmp_path).handle(conn))

    # nothing fetched: no `need` was sent (keep.txt already matched)
    assert [m["type"] for m in conn.sent] == ["hello", "result"]
    assert not (mirror / "stale.txt").exists()
    assert conn.sent[-1]["ok"] is True


def test_failed_command_reports_not_ok(tmp_path):
    task = {"type": "task", "task_id": "t1", "project": "p",
            "command": "exit 3", "manifest": {}}
    conn = FakeConn([{"type": "hello_ok", "name": "mac"}, task])
    asyncio.run(_runtime(tmp_path).handle(conn))
    result = conn.sent[-1]
    assert result["ok"] is False
    assert result["returncode"] == 3


def test_sync_error_aborts_task_without_running(tmp_path):
    task = {"type": "task", "task_id": "t1", "project": "p",
            "command": "touch should_not_exist", "manifest": {"a.txt": "h"}}
    conn = FakeConn([{"type": "hello_ok", "name": "mac"}, task])
    conn.replies["need"] = lambda msg: [
        {"type": "file", "task_id": "t1", "path": "a.txt", "data": None, "error": "boom"}
    ]
    asyncio.run(_runtime(tmp_path).handle(conn))
    result = conn.sent[-1]
    assert result["ok"] is False
    assert "sync failed" in result["stderr"]
    assert not (tmp_path / "mirrors" / "p" / "should_not_exist").exists()


def test_preserve_keeps_artifacts_and_excludes_them_from_changed(tmp_path):
    mirror = tmp_path / "mirrors" / "proj"
    mirror.mkdir(parents=True)
    (mirror / "keep.txt").write_text("same")
    (mirror / "hello.bin").write_text("built earlier")  # artifact, not in source
    keep_hash = manifest.scan(mirror)["keep.txt"]

    task = {
        "type": "task",
        "task_id": "t1",
        "project": "proj",
        "command": "true",
        "manifest": {"keep.txt": keep_hash},
        "preserve": ["*.bin"],
    }
    conn = FakeConn([{"type": "hello_ok", "name": "mac"}, task])
    asyncio.run(_runtime(tmp_path).handle(conn))

    assert (mirror / "hello.bin").exists()  # survived the sync
    result = conn.sent[-1]
    assert result["ok"] is True
    # untouched preserved artifact must not show up as "added" every run
    assert result["changed"] == {"added": [], "modified": [], "deleted": []}


def test_fetch_returns_files_and_errors_then_done(tmp_path):
    mirror = tmp_path / "mirrors" / "proj"
    mirror.mkdir(parents=True)
    (mirror / "artifact.txt").write_text("payload")

    fetch = {"type": "fetch", "task_id": "f1", "project": "proj",
             "paths": ["artifact.txt", "missing.txt", "../escape"]}
    conn = FakeConn([{"type": "hello_ok", "name": "mac"}, fetch])
    asyncio.run(_runtime(tmp_path).handle(conn))

    frames = [m for m in conn.sent if m.get("task_id") == "f1"]
    by_path = {f["path"]: f for f in frames if f["type"] == "file"}
    assert base64.b64decode(by_path["artifact.txt"]["data"]) == b"payload"
    assert by_path["missing.txt"]["data"] is None and by_path["missing.txt"]["error"]
    assert by_path["../escape"]["data"] is None and by_path["../escape"]["error"]
    assert frames[-1]["type"] == "fetch_done"


def test_fetch_rejects_oversized_files(tmp_path, monkeypatch):
    from platform_client import manifest as mmod

    monkeypatch.setattr(mmod, "MAX_FILE_BYTES", 4)
    mirror = tmp_path / "mirrors" / "proj"
    mirror.mkdir(parents=True)
    (mirror / "big.bin").write_text("way too large")

    fetch = {"type": "fetch", "task_id": "f1", "project": "proj", "paths": ["big.bin"]}
    conn = FakeConn([{"type": "hello_ok", "name": "mac"}, fetch])
    asyncio.run(_runtime(tmp_path).handle(conn))

    frame = next(m for m in conn.sent if m.get("type") == "file")
    assert frame["data"] is None
    assert "too large" in frame["error"]


def test_mirror_reports_manifest_or_absence(tmp_path):
    mirror = tmp_path / "mirrors" / "proj"
    mirror.mkdir(parents=True)
    (mirror / "artifact.txt").write_text("payload")

    frames = [
        {"type": "mirror", "task_id": "m1", "project": "proj"},
        {"type": "mirror", "task_id": "m2", "project": "never-synced"},
    ]
    conn = FakeConn([{"type": "hello_ok", "name": "mac"}, *frames])
    asyncio.run(_runtime(tmp_path).handle(conn))

    results = {m["task_id"]: m for m in conn.sent if m.get("type") == "mirror_result"}
    assert results["m1"]["exists"] is True
    assert set(results["m1"]["manifest"]) == {"artifact.txt"}
    assert results["m2"] == {
        "type": "mirror_result", "task_id": "m2", "exists": False, "manifest": {},
    }


def test_clean_deletes_the_mirror(tmp_path):
    mirror = tmp_path / "mirrors" / "proj"
    mirror.mkdir(parents=True)
    (mirror / "artifact.txt").write_text("payload")

    clean = {"type": "clean", "task_id": "c1", "project": "proj"}
    conn = FakeConn([{"type": "hello_ok", "name": "mac"}, clean])
    asyncio.run(_runtime(tmp_path).handle(conn))

    done = next(m for m in conn.sent if m.get("type") == "clean_done")
    assert done["task_id"] == "c1"
    assert done["ok"] is True
    assert not mirror.exists()
    # cleaning an absent mirror is fine too
    conn2 = FakeConn([{"type": "hello_ok", "name": "mac"}, clean])
    asyncio.run(_runtime(tmp_path).handle(conn2))
    assert next(m for m in conn2.sent if m.get("type") == "clean_done")["ok"] is True


def test_project_name_cannot_traverse_mirrors_root(tmp_path):
    task = {"type": "task", "task_id": "t1", "project": "../../evil",
            "command": "true", "manifest": {}}
    conn = FakeConn([{"type": "hello_ok", "name": "mac"}, task])
    asyncio.run(_runtime(tmp_path).handle(conn))
    assert (tmp_path / "mirrors" / "evil").is_dir()
    assert not (tmp_path / "evil").exists()
