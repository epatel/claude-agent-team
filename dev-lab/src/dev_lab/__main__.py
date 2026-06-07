"""Entry point for the dev lab supervisor.

M0: a runnable stub. The Claude Agent SDK loop arrives in M1.
"""

from __future__ import annotations

from . import __version__


def main() -> int:
    print(f"dev-lab {__version__} — skeleton (M0). Agent loop not yet implemented.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
