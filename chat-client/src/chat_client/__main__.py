"""Entry point for the chat client.

M0: a runnable stub. The control surface arrives in M3.
"""

from __future__ import annotations

from . import __version__


def main() -> int:
    print(f"chat-client {__version__} — skeleton (M0). Control surface not yet implemented.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
