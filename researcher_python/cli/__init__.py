"""Command-line entry point for researcher-python.

Thin by house convention: argument parsing and dispatch live here, real work
belongs in the package modules.  The console script is
``researcher-python = "researcher_python.cli:main"`` (see ``pyproject.toml``),
and the repo-root ``cli.py`` delegates to the same :func:`main`.

Framework: **argparse**, as the original single-file CLI used — no new
dependency is introduced, and ``build_parser``/``main`` keep their signatures
so the pre-existing ``--version``/``--log-level`` behaviour is unchanged.

Wiring rules shared by every subcommand (5b reuses them):

* Data goes to stdout; diagnostics and errors go to stderr.
* Exit codes: ``0`` success, ``1`` runtime failure, ``2`` usage/configuration.
* One handler turns any :class:`~researcher_python.errors.ResearcherError`
  into a single line on stderr — no traceback unless ``--log-level DEBUG`` —
  and picks the exit code (``ConfigurationError`` -> 2, anything else -> 1).
  Unexpected exceptions still raise, so Python prints their traceback.
* Settings come from :func:`researcher_python.config.load_settings`.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence

from .. import __version__
from ..config import Settings, load_settings
from ..errors import ConfigurationError, ResearcherError
from ..logging_config import setup_logging
from ..models import SORT_MODES
from .commands import cmd_download, cmd_get, cmd_info, cmd_search, cmd_sources

__all__ = ["build_parser", "main"]

_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]

Handler = Callable[[argparse.Namespace, Settings], int]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="researcher-python",
        description=(
            "researcher-python — one pipeline across arXiv, OpenAlex, "
            "Semantic Scholar, Crossref and PubMed."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=_LOG_LEVELS,
        help="logging level for the package logger (default: INFO)",
    )

    sub = parser.add_subparsers(dest="command")

    info = sub.add_parser(
        "info", help="print package version, Python version and settings"
    )
    info.add_argument(
        "--format", choices=("table", "json"), default="table", help="output format"
    )

    sources = sub.add_parser(
        "sources", help="list registered sources, capabilities and status"
    )
    sources.add_argument(
        "--format", choices=("table", "json"), default="table", help="output format"
    )

    search = sub.add_parser("search", help="search sources and print merged results")
    search.add_argument("query", help="free-text query")
    search.add_argument(
        "--source",
        action="append",
        dest="sources",
        metavar="NAME",
        default=None,
        help="source to query (repeatable; default: every source)",
    )
    search.add_argument(
        "--max",
        type=int,
        default=None,
        metavar="N",
        help="per-source result cap (default: settings.max_results)",
    )
    search.add_argument(
        "--sort", default="relevance", choices=list(SORT_MODES), help="result order"
    )
    search.add_argument(
        "--format",
        choices=("table", "json", "jsonl"),
        default="table",
        help="output format",
    )
    search.add_argument(
        "--out",
        metavar="FILE",
        default=None,
        help="write JSON/JSONL to FILE instead of stdout",
    )

    get = sub.add_parser("get", help="fetch one record by identifier")
    get.add_argument("identifier", help="DOI, arXiv id or PMID")
    get.add_argument(
        "--source", dest="source", metavar="NAME", default=None, help="use only this source"
    )
    get.add_argument(
        "--format", choices=("table", "json"), default="table", help="output format"
    )

    download = sub.add_parser("download", help="download PDFs for identifiers")
    download.add_argument(
        "identifiers", nargs="+", metavar="IDENTIFIER", help="DOI, arXiv id or PMID"
    )
    download.add_argument(
        "--output-dir",
        dest="output_dir",
        metavar="DIR",
        default=None,
        help="where to write PDFs (default: settings.output_dir)",
    )
    download.add_argument(
        "--overwrite", action="store_true", help="replace an existing file"
    )
    download.add_argument(
        "--source", dest="source", metavar="NAME", default=None, help="use only this source"
    )

    return parser


#: Registry key -> the function that runs it.
_HANDLERS: dict[str, Handler] = {
    "info": cmd_info,
    "sources": cmd_sources,
    "search": cmd_search,
    "get": cmd_get,
    "download": cmd_download,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level)

    if not args.command:
        parser.print_help()
        return 0

    try:
        settings = load_settings()
        return int(_HANDLERS[args.command](args, settings))
    except ResearcherError as exc:
        return _report(exc, args.log_level)


def _report(exc: ResearcherError, log_level: str) -> int:
    """Print one line on stderr and return the right exit code.

    ``ConfigurationError`` means *the invocation is wrong* (exit 2); every
    other ``ResearcherError`` means *the work failed* (exit 1).
    """
    if log_level.upper() == "DEBUG":
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
    else:
        # Sources legitimately emit multi-line detail (e.g. one line per failed
        # candidate); the CLI contract is a single line.
        print(" ".join(str(exc).split()) or type(exc).__name__, file=sys.stderr)
    return 2 if isinstance(exc, ConfigurationError) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
