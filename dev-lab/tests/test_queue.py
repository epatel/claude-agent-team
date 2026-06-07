import json

from dev_lab.queue import FileQueue


def test_enqueue_claim_complete(tmp_path):
    q = FileQueue(tmp_path)
    job = q.enqueue("do x", repo="/r")
    assert q.counts()["pending"] == 1

    claimed = q.claim()
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.instruction == "do x"
    assert claimed.repo == "/r"
    assert q.counts()["running"] == 1

    q.complete(claimed)
    assert q.counts() == {"pending": 0, "running": 0, "done": 1, "failed": 0}


def test_claim_empty_returns_none(tmp_path):
    assert FileQueue(tmp_path).claim() is None


def test_fail_records_error(tmp_path):
    q = FileQueue(tmp_path)
    q.enqueue("boom")
    claimed = q.claim()
    q.fail(claimed, "bad things happened")

    assert q.counts()["failed"] == 1
    failed_file = next((tmp_path / "failed").glob("*.json"))
    assert json.loads(failed_file.read_text())["error"] == "bad things happened"


def test_recover_requeues_running(tmp_path):
    q = FileQueue(tmp_path)
    q.enqueue("x")
    q.claim()
    assert q.counts()["running"] == 1

    moved = q.recover()
    assert moved == 1
    assert q.counts()["pending"] == 1
    assert q.counts()["running"] == 0
