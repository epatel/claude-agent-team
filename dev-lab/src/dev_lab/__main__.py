"""Entry point for the dev lab.

M1: run a single instruction against a local git clone (branch -> agent -> commit).
The long-running supervisor / control surface arrive in later milestones.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import __version__
from .config import load_config
from .lab import run_once


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dev-lab",
        description="Autonomous dev lab (M1: one instruction -> one commit).",
    )
    parser.add_argument("instruction", nargs="?", help="what the lab should do")
    parser.add_argument("--repo", help="path to the local git clone to work in")
    parser.add_argument(
        "--no-commit", action="store_true", help="let the agent edit but do not commit"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not args.instruction:
        print(f'dev-lab {__version__} (M1). Usage: dev-lab "<instruction>" --repo <path>')
        return 0
    if not args.repo:
        print("error: --repo <path to local git clone> is required to run a task", file=sys.stderr)
        return 2

    config = load_config()
    result = asyncio.run(
        run_once(args.instruction, repo_path=args.repo, config=config, commit=not args.no_commit)
    )

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
    return 1 if result.agent.is_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
