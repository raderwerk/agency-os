"""Startpunt: `python -m agency_os <commando>`."""

from __future__ import annotations

import sys

from agency_os.app.cli import main as cli_main


def main() -> int:
    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
