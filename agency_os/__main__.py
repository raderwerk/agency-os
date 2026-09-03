"""Startpunt: `python -m agency_os <commando>`."""

from __future__ import annotations

import sys


def main() -> int:
    """Laadt de CLI en geeft een leesbare fout als een module nog ontbreekt."""
    try:
        from agency_os.app.cli import main as cli_main
    except ImportError as exc:  # onderdeel A of B is nog niet aanwezig
        print(f"agency_os is nog niet compleet: {exc}", file=sys.stderr)
        return 2
    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
