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
from ..sources.base import SEARCH_FIELDS
from .commands import (
    cmd_bibliography,
    cmd_citations,
    cmd_download,
    cmd_extract,
    cmd_get,
    cmd_handoff,
    cmd_info,
    cmd_label,
    cmd_matrix,
    cmd_prescreen,
    cmd_prisma,
    cmd_references,
    cmd_screen,
    cmd_search,
    cmd_snowball,
    cmd_sources,
    cmd_synthesis,
    cmd_summary,
    cmd_web_search,
    cmd_workspace,
)

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
        choices=_CITE_FORMATS,
        default="table",
        help="output format (harvard/bibtex/ris/csl render the result as references)",
    )
    search.add_argument("--from", dest="year_from", type=int, default=None, metavar="YEAR",
                        help="only works published in YEAR or later")
    search.add_argument("--to", dest="year_to", type=int, default=None, metavar="YEAR",
                        help="only works published in YEAR or earlier")
    search.add_argument("--field", choices=SEARCH_FIELDS, default="any",
                        help="restrict matching to a field (default: any)")
    search.add_argument("--type", "--types", dest="work_types", action="append",
                        metavar="TYPE",
                        help="only these work types (repeatable or comma-separated; "
                        "source spellings like journal-article are accepted). "
                        "An explicit list replaces the default exclusions")
    search.add_argument("--exclude-type", dest="exclude_types", action="append",
                        metavar="TYPE",
                        help="also exclude this work type (repeatable); default "
                        "exclusions are grant and standard")
    search.add_argument("--no-default-exclude", dest="no_default_exclude",
                        action="store_true",
                        help="do not exclude grants/standards by default")
    enrich_group = search.add_mutually_exclusive_group()
    enrich_group.add_argument("--enrich", dest="force_enrich", action="store_true",
                              help="force the OpenAlex missing-count/abstract "
                              "backfill even when openalex was not searched")
    enrich_group.add_argument("--no-enrich", dest="no_enrich", action="store_true",
                              help="skip the OpenAlex backfill")
    search.add_argument("--min-score", dest="min_score", type=float, default=None,
                        metavar="F", help="drop merged records scoring below F (0.0-1.0); "
                        "the local relevance score from researcher_python.relevance "
                        "(default: no floor)")
    search.add_argument("--min-citations", dest="min_citations", type=int, default=None,
                        metavar="N", help="drop records with fewer than N citations "
                        "(unknown counts are kept); a crude standing proxy — see the "
                        "matrix's citations_per_year column for the age-normalised view")
    search.add_argument("--top-ratio", dest="top_ratio", type=float, default=None,
                        metavar="R", help="keep records scoring at least R x the "
                        "best score (0.0<R<=1.0); scale-free, so unlike --min-score "
                        "it cuts the tail however long the query is")
    search.add_argument("--accessed", default="", metavar="DATE",
                        help="access date used in harvard 'Available at' links")
    search.add_argument(
        "--search-log",
        dest="search_log",
        metavar="FILE",
        default=None,
        help="append a provenance row (query, scope, sources, counts, terms) "
        "to this Markdown table — the round log the module's lab asks for; "
        "created with a header if absent",
    )
    search.add_argument(
        "--out",
        metavar="FILE",
        default=None,
        help="write JSON/JSONL to FILE instead of stdout",
    )
    _add_workspace_flag(
        search,
        "persist this search into DIR (results.json + search log + meta); "
        "dedupes against prior rounds and reports new/seen/screened",
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

    screen = sub.add_parser(
        "screen",
        aliases=["label"],
        help="screen: set labels / notes, one record or a whole batch",
    )
    screen.add_argument(
        "from_file", nargs="?", metavar="FILE", help="a saved .json result set"
    )
    _add_workspace_flag(
        screen, "screen the canonical set in DIR instead of a standalone file"
    )
    screen.add_argument("--set", dest="set", default=None,
                        choices=("unreviewed", "core", "general", "excluded"),
                        help="screening label to apply")
    screen.add_argument("--reason", default="", metavar="TEXT",
                        help="why (kept only with --set excluded)")
    screen.add_argument("--note", default="", metavar="TEXT", help="append a dated note")
    screen.add_argument("--id", action="append", default=None, metavar="ID",
                        help="only this record (repeatable; default: all)")
    screen.add_argument(
        "--batch",
        metavar="FILE",
        default=None,
        help="apply many updates from a JSON array / JSONL file ('-' = stdin); "
        "each entry is {id|ids, label, reason, note}",
    )
    screen.add_argument(
        "--lenient",
        action="store_true",
        help="apply valid batch updates and report invalid ones instead of "
        "refusing the whole batch",
    )
    screen.add_argument("--out", metavar="FILE", default=None,
                        help="write elsewhere (default: rewrite in place)")

    prescreen_parser = sub.add_parser(
        "prescreen", help="flag obvious exclusions by rule (report by default)"
    )
    prescreen_parser.add_argument(
        "from_file", nargs="?", metavar="FILE", help="a saved .json result set"
    )
    _add_workspace_flag(
        prescreen_parser, "pre-screen the canonical set in DIR"
    )
    prescreen_parser.add_argument("--year-from", dest="year_from", type=int, default=None)
    prescreen_parser.add_argument("--year-to", dest="year_to", type=int, default=None)
    prescreen_parser.add_argument(
        "--exclude-types",
        dest="exclude_types",
        default=None,
        metavar="A,B",
        help="work types to exclude (default: grant,standard,dataset)",
    )
    prescreen_parser.add_argument(
        "--rules",
        default=None,
        metavar="A,B",
        help="subset of no-title,no-abstract,type,outside-year",
    )
    prescreen_parser.add_argument(
        "--allow-no-abstract", dest="allow_no_abstract", action="store_true",
        help="do not flag records with no abstract",
    )
    prescreen_parser.add_argument(
        "--allow-no-title", dest="allow_no_title", action="store_true",
        help="do not flag records with no title",
    )
    prescreen_parser.add_argument(
        "--apply", action="store_true", help="write the flagged records as excluded"
    )
    prescreen_parser.add_argument("--limit", type=int, default=50, metavar="N",
                                  help="max candidates to print (default: 50)")
    prescreen_parser.add_argument("--format", choices=("table", "json"), default="table")
    prescreen_parser.add_argument(
        "--out",
        metavar="FILE",
        default=None,
        help="write the rewritten result set (with --apply) or the report "
        "(without); on a workspace it always writes the report",
    )

    snowball_parser = sub.add_parser(
        "snowball", help="expand a core set through references/citations"
    )
    _add_workspace_flag(snowball_parser, "workspace whose core set to expand")
    snowball_parser.add_argument(
        "--direction", default="both", choices=("references", "citations", "both"),
        help="which edge to follow (default: both)",
    )
    snowball_parser.add_argument("--id", action="append", default=None, metavar="ID",
                                 help="core id (repeatable; default: records labelled core)")
    snowball_parser.add_argument("--core-label", dest="core_label", default="core",
                                 choices=("core", "general"),
                                 help="label that defines the core set (default: core)")
    snowball_parser.add_argument("--min-links", dest="min_links", type=int, default=1,
                                 metavar="N", help="require N core papers to link a candidate")
    snowball_parser.add_argument("--limit", type=int, default=None, metavar="N",
                                 help="per-core cap passed to each citation source")
    snowball_parser.add_argument("--max-results", dest="max_results", type=int, default=25,
                                 metavar="N", help="max candidates to show (default: 25)")
    snowball_parser.add_argument("--no-persist", dest="no_persist", action="store_true",
                                 help="preview only; do not merge into the workspace")
    snowball_parser.add_argument("--format", choices=("table", "json"), default="table")
    snowball_parser.add_argument("--out", metavar="FILE", default=None)

    prisma_parser = sub.add_parser(
        "prisma", help="PRISMA-style flow counts from the log and labels"
    )
    prisma_parser.add_argument("path", help="workspace directory")
    prisma_parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    prisma_parser.add_argument("--out", metavar="FILE", default=None)

    handoff_parser = sub.add_parser(
        "handoff",
        help="write a versioned research bundle for a paper-writing tool",
    )
    handoff_parser.add_argument(
        "source", help="workspace directory, or a saved .json/.jsonl result file"
    )
    handoff_parser.add_argument("--out", required=True, metavar="DIR")
    handoff_parser.add_argument(
        "--labels",
        default="core,general",
        metavar="A,B",
        help="triage labels to include (default: core,general)",
    )
    handoff_parser.add_argument(
        "--allow-unscreened",
        dest="require_screening",
        action="store_false",
        default=True,
        help="write the bundle even if some records are still 'unreviewed'",
    )
    handoff_parser.add_argument(
        "--no-prisma",
        dest="include_prisma",
        action="store_false",
        default=True,
        help="omit prisma.md (a result-file source has no log anyway)",
    )
    handoff_parser.add_argument("--force", action="store_true", help="overwrite an existing bundle")
    handoff_parser.add_argument(
        "--format", choices=("json", "text"), default="json", help="receipt format"
    )

    _add_citation_parsers(sub)
    _add_bibliography_parser(sub)
    _add_matrix_parsers(sub)
    _add_web_search_parser(sub)
    _add_extract_parser(sub)
    _add_workspace_parser(sub)

    return parser


# ---------------------------------------------------------------------- #
# Parser helpers
# ---------------------------------------------------------------------- #

#: Formats that render a citation list, and ``table`` as the terminal view.
_CITE_FORMATS = ("table", "json", "jsonl", "harvard", "bibtex", "ris", "csl")

#: Formats that render a *bibliographic reference list* (no table/JSONL views).
_BIB_FORMATS = ("harvard", "bibtex", "ris", "csl")


def _citation_parser(sub, name: str, help_text: str) -> argparse.ArgumentParser:
    """One of the two symmetric citation subcommands."""
    parser = sub.add_parser(name, help=help_text)
    parser.add_argument("identifier", help="DOI, arXiv id, PMID or ISBN")
    parser.add_argument(
        "--source",
        action="append",
        dest="sources",
        metavar="NAME",
        default=None,
        help="source to query (repeatable; default: every capable source)",
    )
    parser.add_argument("--get-source", dest="get_source", metavar="NAME", default=None,
                        help="resolve the subject record with only this source")
    parser.add_argument("--max", type=int, default=None, metavar="N",
                        help="per-source cap (default: settings.max_results)")
    parser.add_argument("--order", default="canonical",
                        choices=("canonical", "year", "citations", "title"),
                        help="result order (default: canonical)")
    parser.add_argument("--strict", action="store_true",
                        help="fail if any requested source fails")
    parser.add_argument("--format", choices=_CITE_FORMATS, default="table",
                        help="output format (default: table)")
    parser.add_argument("--accessed", default="", metavar="DATE",
                        help="access date used in harvard 'Available at' links")
    parser.add_argument("--out", metavar="FILE", default=None,
                        help="write to FILE instead of stdout")
    return parser


def _add_citation_parsers(sub) -> None:
    _citation_parser(sub, "references", "list the works a paper cites")
    _citation_parser(sub, "citations", "list the works that cite a paper")


def _add_workspace_flag(parser, help_text: str) -> None:
    """Add the shared ``--workspace DIR`` flag to a subcommand."""
    parser.add_argument(
        "--workspace",
        dest="workspace",
        metavar="DIR",
        default=None,
        help=help_text,
    )


def _add_bibliography_parser(sub) -> None:
    parser = sub.add_parser(
        "bibliography",
        help="render a reference list from ids, a query, or a saved result file",
    )
    parser.add_argument("identifiers", nargs="*", metavar="IDENTIFIER",
                        help="DOI, arXiv id, PMID or ISBN")
    parser.add_argument("--query", default=None, metavar="TEXT",
                        help="search for these instead of resolving identifiers")
    parser.add_argument("--from-file", dest="from_file", metavar="FILE", default=None,
                        help="read a saved .json/.jsonl result set (reproducible)")
    parser.add_argument("--source", action="append", dest="sources", metavar="NAME",
                        default=None, help="source for --query (repeatable)")
    parser.add_argument("--get-source", dest="get_source", metavar="NAME", default=None,
                        help="resolve identifiers with only this source")
    parser.add_argument("--max", type=int, default=None, metavar="N",
                        help="per-source cap for --query")
    parser.add_argument("--format", choices=_BIB_FORMATS, default="harvard",
                        help="reference-list style (default: harvard, i.e. Cite Them Right)")
    parser.add_argument("--accessed", default="", metavar="DATE",
                        help="access date for harvard 'Available at' links")
    parser.add_argument("--out", metavar="FILE", default=None, help="write to FILE")
    _add_workspace_flag(
        parser, "render the canonical set in DIR instead of --from-file"
    )


def _add_matrix_parsers(sub) -> None:
    for name, help_text in (
        ("matrix", "Literature Matrix: one row per paper"),
        ("synthesis", "Synthesis Matrix: a dimension cross-tabbed against screening"),
    ):
        parser = sub.add_parser(name, help=help_text)
        parser.add_argument("identifiers", nargs="*", metavar="IDENTIFIER")
        parser.add_argument("--query", default=None, metavar="TEXT")
        parser.add_argument("--from-file", dest="from_file", metavar="FILE", default=None)
        parser.add_argument("--source", action="append", dest="sources", metavar="NAME",
                            default=None)
        parser.add_argument("--get-source", dest="get_source", metavar="NAME", default=None)
        parser.add_argument("--max", type=int, default=None, metavar="N")
        parser.add_argument("--format", choices=("csv", "markdown"), default="csv")
        parser.add_argument("--require-screening", dest="require_screening",
                            action="store_true",
                            help="refuse to render an unscreened set (any record "
                            "still 'unreviewed'); a matrix records screening "
                            "decisions, so this is the lab deliverable, not a dump")
        parser.add_argument("--out", metavar="FILE", default=None)
        _add_workspace_flag(
            parser, "render the canonical set in DIR instead of --from-file"
        )
        if name == "matrix":
            parser.add_argument("--columns", default=None, metavar="A,B,C",
                                help="comma-separated matrix columns")
        else:
            parser.add_argument("--dimension", default="year",
                                choices=("label", "year", "type", "venue", "open_access"),
                                help="what to cross-tab (default: year)")

    summary = sub.add_parser("summary", help="screening counts per triage label")
    summary.add_argument("identifiers", nargs="*", metavar="IDENTIFIER")
    summary.add_argument("--query", default=None, metavar="TEXT")
    summary.add_argument("--from-file", dest="from_file", metavar="FILE", default=None)
    summary.add_argument("--source", action="append", dest="sources", metavar="NAME",
                         default=None)
    summary.add_argument("--get-source", dest="get_source", metavar="NAME", default=None)
    summary.add_argument("--max", type=int, default=None, metavar="N")
    summary.add_argument("--format", choices=("table", "json"), default="table")
    summary.add_argument("--out", metavar="FILE", default=None)
    _add_workspace_flag(
        summary, "summarise the canonical set in DIR instead of --from-file"
    )


def _add_web_search_parser(sub) -> None:
    parser = sub.add_parser("web-search", help="search Stack Overflow / Reddit records")
    parser.add_argument("query", help="free-text query")
    parser.add_argument("--source", default="stackoverflow",
                        choices=("stackoverflow", "reddit"), help="web source")
    parser.add_argument("--max", type=int, default=None, metavar="N")
    parser.add_argument("--sort", default=None, metavar="KEY", help="source-specific sort")
    parser.add_argument("--site", default=None, metavar="SITE",
                        help="Stack Exchange site (e.g. serverfault)")
    parser.add_argument("--subreddit", default=None, metavar="NAME", help="reddit scope")
    parser.add_argument("--time", default=None, metavar="RANGE",
                        help="reddit time window (hour|day|week|month|year|all)")
    parser.add_argument("--format", choices=("table", "json", "bibtex", "ris", "csl"),
                        default="table")
    parser.add_argument("--accessed", default="", metavar="DATE")
    parser.add_argument("--out", metavar="FILE", default=None)


def _add_extract_parser(sub) -> None:
    parser = sub.add_parser(
        "extract", help="pull text or page-anchored quotes out of downloaded PDFs"
    )
    parser.add_argument("paths", nargs="+", help="PDF files, or directories of them")
    parser.add_argument("--pages", default=None, metavar="SPEC",
                        help="page selection, e.g. 1-5,10 (default: whole file)")
    parser.add_argument("--quotes", action="store_true",
                        help="capture quotes instead of raw text")
    parser.add_argument("--terms", default="", metavar="WORDS",
                        help="comma-separated terms a quote must contain")
    parser.add_argument("--window", type=int, default=240, metavar="CHARS",
                        help="characters of context around a match (default: 240)")
    parser.add_argument("--limit", type=int, default=10, metavar="N",
                        help="max quotes per file (default: 10)")
    parser.add_argument("--out", metavar="FILE", default=None,
                        help="write quotes as JSON, or append text, to FILE")


def _add_workspace_parser(sub) -> None:
    """The ``workspace`` command group: init / status / log / round / export / import."""
    parser = sub.add_parser(
        "workspace",
        help="manage a workspace (results.json + search log + meta)",
    )
    actions = parser.add_subparsers(dest="workspace_command", required=True)

    init = actions.add_parser("init", help="create a workspace directory")
    init.add_argument("path", help="workspace directory (absolute, or under RESEARCHER_HOME)")
    init.add_argument("--topic", default="", metavar="TEXT")

    status = actions.add_parser("status", help="counts, round, provenance and paths")
    status.add_argument("path")
    status.add_argument("--format", choices=("table", "json"), default="table")

    log = actions.add_parser("log", help="print the provenance log")
    log.add_argument("path")
    log.add_argument("--format", choices=("md", "jsonl", "json"), default="md")
    log.add_argument("--round", type=int, default=None, metavar="N")

    round_parser = actions.add_parser("round", help="show or advance the round")
    round_parser.add_argument("path")
    action = round_parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--next", action="store_true", help="snapshot and advance")
    action.add_argument("--set", type=int, default=None, metavar="N")
    round_parser.add_argument("--label", default="", metavar="TEXT")

    export = actions.add_parser("export", help="write the canonical set to a file")
    export.add_argument("path")
    export.add_argument("--out", required=True, metavar="FILE")
    export.add_argument("--format", choices=("json", "jsonl"), default="json")

    adopt = actions.add_parser(
        "import", help="adopt pre-workspace results (and optionally a legacy log)"
    )
    adopt.add_argument("path")
    adopt.add_argument("--results", required=True, metavar="FILE")
    adopt.add_argument("--log", default=None, metavar="FILE")
    adopt.add_argument("--round", type=int, default=1, metavar="N")
    adopt.add_argument("--topic", default="", metavar="TEXT")
    adopt.add_argument(
        "--mode",
        choices=("snapshot", "merge"),
        default="snapshot",
        help="snapshot replaces the set; merge folds this file into it "
        "(use merge for disjoint legacy round files)",
    )
    adopt.add_argument("--dry-run", dest="dry_run", action="store_true")


#: Registry key -> the function that runs it.
_HANDLERS: dict[str, Handler] = {
    "info": cmd_info,
    "workspace": cmd_workspace,
    "sources": cmd_sources,
    "search": cmd_search,
    "get": cmd_get,
    "download": cmd_download,
    "references": cmd_references,
    "citations": cmd_citations,
    "bibliography": cmd_bibliography,
    "matrix": cmd_matrix,
    "synthesis": cmd_synthesis,
    "summary": cmd_summary,
    "web-search": cmd_web_search,
    "extract": cmd_extract,
    "screen": cmd_screen,
    "label": cmd_label,
    "prescreen": cmd_prescreen,
    "snowball": cmd_snowball,
    "prisma": cmd_prisma,
    "handoff": cmd_handoff,
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
