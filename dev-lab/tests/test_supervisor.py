import asyncio

from dev_lab import db
from dev_lab.agent import AgentResult
from dev_lab.config import Config
from dev_lab.lab import RunResult
from dev_lab.queue import FileQueue
from dev_lab.supervisor import serve


def _result() -> RunResult:
    return RunResult(
        branch="lab/x",
        base_sha="a" * 40,
        commit_sha="b" * 40,
        committed=True,
        agent=AgentResult(summary="ok", num_turns=1, is_error=False, total_cost_usd=0.0),
    )


def test_serve_processes_jobs(tmp_path):
    q = FileQueue(tmp_path)
    q.enqueue("a", repo="/r")
    q.enqueue("b", repo="/r")

    calls = []

    async def fake(instruction, *, repo_path, config, commit=True):
        calls.append(instruction)
        return _result()

    n = asyncio.run(serve(config=Config(github_token="x"), queue=q, max_jobs=2, run=fake))

    assert n == 2
    assert q.counts()["done"] == 2
    assert sorted(calls) == ["a", "b"]


def test_serve_failing_job_moves_to_failed(tmp_path):
    q = FileQueue(tmp_path)
    q.enqueue("boom", repo="/r")

    async def boom(instruction, *, repo_path, config, commit=True):
        raise RuntimeError("kaboom")

    n = asyncio.run(serve(config=Config(github_token="x"), queue=q, max_jobs=1, run=boom))

    assert n == 1
    assert q.counts()["failed"] == 1
    assert q.counts()["done"] == 0


def test_serve_job_without_repo_fails(tmp_path):
    q = FileQueue(tmp_path)
    q.enqueue("no repo")  # repo is None and no default_repo

    async def fake(instruction, *, repo_path, config, commit=True):
        return _result()

    n = asyncio.run(
        serve(config=Config(github_token="x"), queue=q, default_repo=None, max_jobs=1, run=fake)
    )

    assert n == 1
    assert q.counts()["failed"] == 1


def test_serve_records_runs_in_db(tmp_path):
    q = FileQueue(tmp_path / "q")
    q.enqueue("good", repo="/r")
    q.enqueue("bad", repo="/r")
    conn = db.connect(tmp_path / "lab.db")

    async def run(instruction, *, repo_path, config, commit=True):
        if instruction == "bad":
            raise RuntimeError("boom")
        return _result()

    asyncio.run(serve(config=Config(github_token="x"), queue=q, max_jobs=2, db=conn, run=run))

    rows = conn.execute("SELECT instruction, status FROM runs ORDER BY instruction").fetchall()
    assert {(r["instruction"], r["status"]) for r in rows} == {("bad", "failed"), ("good", "done")}


def test_serve_recovers_inflight_job(tmp_path):
    q = FileQueue(tmp_path)
    q.enqueue("x", repo="/r")
    q.claim()  # leave it in running/, as if the process crashed mid-run

    processed = []

    async def fake(instruction, *, repo_path, config, commit=True):
        processed.append(instruction)
        return _result()

    n = asyncio.run(serve(config=Config(github_token="x"), queue=q, max_jobs=1, run=fake))

    assert n == 1
    assert processed == ["x"]
    assert q.counts()["done"] == 1
