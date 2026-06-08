"""Long-running supervisor: drain the job queue one task at a time, forever.

This is the M2 core (see cards/dev-lab.md, cards/deployment.md). It survives
per-job errors (logs and moves the job to failed/) so the lab keeps running, and
recovers in-flight jobs after a crash via the queue's ``recover()``. ``run`` is a
parameter so the loop can be tested without invoking the real agent.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import Config
from .db import record_run
from .events import EventBus
from .lab import RunResult, run_once
from .queue import FileQueue

logger = logging.getLogger("dev_lab.supervisor")

RunOnce = Callable[..., Awaitable[RunResult]]


async def _sleep_or_stop(seconds: float, stop_event: asyncio.Event | None) -> None:
    if stop_event is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def serve(
    *,
    config: Config,
    queue: FileQueue,
    default_repo: str | Path | None = None,
    poll_interval: float = 5.0,
    max_jobs: int | None = None,
    stop_event: asyncio.Event | None = None,
    db: sqlite3.Connection | None = None,
    bus: EventBus | None = None,
    run: RunOnce = run_once,
) -> int:
    """Process queued jobs until stopped; return the number of jobs processed.

    ``max_jobs`` bounds the run (and, when set, exits as soon as the queue drains)
    — mainly for tests. With ``max_jobs=None`` the loop idles between polls and
    runs until ``stop_event`` is set (SIGTERM/SIGINT in the CLI).
    """
    recovered = queue.recover()
    if recovered:
        logger.info("recovered %d in-flight job(s) back to pending", recovered)

    processed = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        if max_jobs is not None and processed >= max_jobs:
            break

        job = queue.claim()
        if job is None:
            if max_jobs is not None:
                break  # bounded run: drain and exit instead of idling
            await _sleep_or_stop(poll_interval, stop_event)
            continue

        repo = job.repo or (str(default_repo) if default_repo else None)
        if not repo:
            msg = "no repo specified (job.repo and default_repo both unset)"
            queue.fail(job, msg)
            if db is not None:
                record_run(
                    db, job_id=job.id, instruction=job.instruction, repo=None,
                    status="failed", error=msg,
                )
            if bus is not None:
                await bus.publish({"type": "job_failed", "job_id": job.id, "error": msg})
            logger.error("job %s failed: no repo specified", job.id)
            processed += 1
            continue

        extra: dict = {}
        if bus is not None:
            await bus.publish(
                {"type": "job_running", "job_id": job.id, "instruction": job.instruction}
            )

            async def _on_event(event: dict, _job_id: str = job.id) -> None:
                await bus.publish({**event, "job_id": _job_id})

            extra["on_event"] = _on_event

        logger.info("running job %s: %s", job.id, job.instruction)
        try:
            result = await run(job.instruction, repo_path=repo, config=config, **extra)
        except Exception as exc:  # noqa: BLE001 — keep the lab alive across any task failure
            queue.fail(job, repr(exc))
            if db is not None:
                record_run(
                    db, job_id=job.id, instruction=job.instruction, repo=repo,
                    status="failed", error=repr(exc),
                )
            if bus is not None:
                await bus.publish({"type": "job_failed", "job_id": job.id, "error": repr(exc)})
            logger.exception("job %s failed", job.id)
        else:
            queue.complete(job)
            if db is not None:
                record_run(
                    db, job_id=job.id, instruction=job.instruction, repo=repo,
                    status="done", branch=result.branch, base_sha=result.base_sha,
                    commit_sha=result.commit_sha, committed=result.committed,
                    cost_usd=result.agent.total_cost_usd,
                )
            if bus is not None:
                await bus.publish(
                    {
                        "type": "job_done",
                        "job_id": job.id,
                        "branch": result.branch,
                        "commit_sha": result.commit_sha,
                        "committed": result.committed,
                    }
                )
            logger.info(
                "job %s done: branch=%s committed=%s", job.id, result.branch, result.committed
            )
        processed += 1

    return processed
