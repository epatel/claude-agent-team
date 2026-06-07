"""File-backed job queue for the supervisor — durable across restarts.

Layout under the queue root (one JSON file per job):

  pending/   jobs waiting to run
  running/   jobs currently being processed (recovered to pending on restart)
  done/      completed jobs
  failed/    jobs that errored (annotated with the error)

A job file is ``{"id", "instruction", "repo" (optional), "created_at"}``. Claiming
a job is an atomic rename from pending/ to running/, so a crash mid-run leaves the
job in running/ where ``recover()`` requeues it.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

_SUBDIRS = ("pending", "running", "done", "failed")


@dataclass(frozen=True)
class Job:
    id: str
    instruction: str
    repo: str | None
    created_at: float
    path: Path


class FileQueue:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        for sub in _SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def _dir(self, name: str) -> Path:
        return self.root / name

    def enqueue(self, instruction: str, *, repo: str | None = None) -> Job:
        job_id = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        data = {"id": job_id, "instruction": instruction, "repo": repo, "created_at": time.time()}
        path = self._dir("pending") / f"{job_id}.json"
        path.write_text(json.dumps(data, indent=2))
        return Job(path=path, **data)

    def recover(self) -> int:
        """Move jobs left in running/ (from a crash) back to pending; return count."""
        moved = 0
        for f in sorted(self._dir("running").glob("*.json")):
            f.rename(self._dir("pending") / f.name)
            moved += 1
        return moved

    def claim(self) -> Job | None:
        """Atomically take the oldest pending job into running/; None if empty."""
        for f in sorted(self._dir("pending").glob("*.json")):
            target = self._dir("running") / f.name
            try:
                f.rename(target)  # atomic on the same filesystem
            except FileNotFoundError:
                continue  # raced with another claim
            data = json.loads(target.read_text())
            return Job(
                id=data["id"],
                instruction=data["instruction"],
                repo=data.get("repo"),
                created_at=data["created_at"],
                path=target,
            )
        return None

    def complete(self, job: Job) -> None:
        job.path.rename(self._dir("done") / job.path.name)

    def fail(self, job: Job, error: str) -> None:
        data = json.loads(job.path.read_text())
        data["error"] = error
        job.path.write_text(json.dumps(data, indent=2))
        job.path.rename(self._dir("failed") / job.path.name)

    def counts(self) -> dict[str, int]:
        return {sub: len(list(self._dir(sub).glob("*.json"))) for sub in _SUBDIRS}
