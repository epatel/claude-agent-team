"""Entry point for the dev lab.

Commands:
  run     — run one instruction now against a local clone (one-shot, M1).
  serve   — long-running supervisor that drains the job queue forever (M2).
  submit  — enqueue an instruction for a running supervisor (M2).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from . import __version__
from .config import load_config
from .db import connect as db_connect
from .lab import run_once
from .queue import FileQueue
from .supervisor import serve

DEFAULT_QUEUE = "dev-lab-queue"
DEFAULT_DB = "dev-lab.db"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dev-lab", description="Autonomous dev lab.")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="run one instruction now (one-shot)")
    run_p.add_argument("instruction")
    run_p.add_argument("--repo", required=True, help="path to the local git clone to work in")
    run_p.add_argument("--no-commit", action="store_true", help="edit but do not commit")

    serve_p = sub.add_parser("serve", help="run the supervisor loop (drains the queue forever)")
    serve_p.add_argument("--repo", required=True, help="default clone for jobs without their own")
    serve_p.add_argument("--queue", default=DEFAULT_QUEUE, help="queue directory")
    serve_p.add_argument("--db", default=DEFAULT_DB, help="SQLite path for run history")
    serve_p.add_argument("--poll", type=float, default=5.0, help="seconds between polls when idle")

    submit_p = sub.add_parser("submit", help="enqueue an instruction for a running supervisor")
    submit_p.add_argument("instruction")
    submit_p.add_argument("--queue", default=DEFAULT_QUEUE, help="queue directory")
    submit_p.add_argument("--repo", help="optional per-job repo override")

    return parser


def _print_run_result(result) -> None:
    print(f"branch:   {result.branch}")
    print(f"base:     {result.base_sha[:12]}")
    if result.committed and result.commit_sha:
        print(f"commit:   {result.commit_sha[:12]}")
    else:
        print("commit:   (none — no changes or --no-commit)")
    if result.agent.total_cost_usd is not None:
        print(f"turns:    {result.agent.num_turns}  cost: ${result.agent.total_cost_usd:.4f}")
    if result.agent.summary:
        print(f"\n{result.agent.summary}")


def _cmd_run(args) -> int:
    config = load_config()
    result = asyncio.run(
        run_once(args.instruction, repo_path=args.repo, config=config, commit=not args.no_commit)
    )
    _print_run_result(result)
    return 1 if result.agent.is_error else 0


def _cmd_serve(args) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = load_config()
    queue = FileQueue(args.queue)
    db = db_connect(args.db)
    log = logging.getLogger("dev_lab")

    async def _main() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        log.info("supervisor up; queue=%s db=%s default_repo=%s", args.queue, args.db, args.repo)
        return await serve(
            config=config,
            queue=queue,
            default_repo=args.repo,
            poll_interval=args.poll,
            stop_event=stop,
            db=db,
        )

    try:
        processed = asyncio.run(_main())
    finally:
        db.close()
    log.info("supervisor stopped after %d job(s)", processed)
    return 0


def _cmd_submit(args) -> int:
    job = FileQueue(args.queue).enqueue(args.instruction, repo=args.repo)
    print(f"queued {job.id} -> {args.queue}/pending")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "submit":
        return _cmd_submit(args)
    print(f"dev-lab {__version__}. Commands: run | serve | submit (see --help).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
