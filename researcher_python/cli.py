"""Command-line entry point for researcher-python.

Thin by house convention: argument parsing lives here, real work belongs in
the package modules.  Run it as `python cli.py <command>` from the repo root
or via the installed `researcher-python` console script.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__
from .logging_config import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="researcher-python",
        description="researcher-python — Python project scaffold (purpose TBD).",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logging level for the package logger (default: INFO)",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("info", help="print package name, version, and install path")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)

    if args.command == "info":
        import researcher_python

        print(f"researcher-python {__version__}")
        print(f"package: {researcher_python.__file__}")
        return 0

    build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
