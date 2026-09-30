"""Subcommand implementations for the ``researcher-python`` CLI.

These are plain ``(args, settings) -> exit code`` functions so
:mod:`researcher_python.cli` can dispatch them and apply one shared error
handler.  Nothing here prints to stderr — errors are raised as
:class:`~researcher_python.errors.ResearcherError` and reported upstream.

Both subcommands make **no network calls**: ``sources`` only *constructs*
each adapter, and ``info`` only reads :class:`~researcher_python.config.Settings`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
from typing import Any

from .. import download, export, pipeline
from .. import __version__
from ..config import ENV_KEYS, Settings
from ..errors import ConfigurationError, ResearcherError, SourceUnavailableError
from ..models import Paper
from ..sources import DEFAULT_SOURCES, create_source, get_source_class, source_names

__all__ = ["cmd_download", "cmd_get", "cmd_info", "cmd_search", "cmd_sources"]

#: ``(field, letter)`` pairs, in the order shown by the table's CAPS column.
_CAP_FIELDS = (
    ("search", "S"),
    ("get", "G"),
    ("references", "R"),
    ("citations", "C"),
    ("pdf", "P"),
)

#: Settings whose value is a secret and must only ever be shown set/unset.
_KEY_FIELDS = (
    "semantic_scholar_api_key",
    "openalex_api_key",
    "ncbi_api_key",
)


# ---------------------------------------------------------------------- #
# Shared rendering
# ---------------------------------------------------------------------- #


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    """Plain-text table — no tabulate, no alignment library."""
    widths = [
        max([len(header)] + [len(str(row[i])) for row in rows])
        for i, header in enumerate(headers)
    ]

    def line(cells: list[Any]) -> str:
        return "  ".join(
            str(cell).ljust(width) for cell, width in zip(cells, widths)
        ).rstrip()

    out = [line(headers), line(["-" * width for width in widths])]
    out.extend(line(row) for row in rows)
    return "\n".join(out)


def _set_unset(value: str) -> str:
    return "set" if value else "unset"


def _line(value: Any) -> str:
    """Collapse a possibly multi-line message onto one line (CLI contract)."""
    return " ".join(str(value).split())


def _env_var_from_error(exc: ConfigurationError) -> str:
    """The env var named by a ``ConfigurationError``, if it names one."""
    match = re.search(r"export (\S+) first", str(exc))
    if match:
        return match.group(1)
    for env_name, field in ENV_KEYS.items():
        if field in str(exc):
            return env_name
    return ""


# ---------------------------------------------------------------------- #
# info
# ---------------------------------------------------------------------- #


def cmd_info(args: argparse.Namespace, settings: Settings) -> int:
    """Print version, Python version and settings (secrets redacted)."""
    payload = _info_payload(settings)
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False))
        return 0

    print(f"researcher-python {payload['version']}")
    print(f"package: {payload['package']}")
    print(f"python: {payload['python']}")
    print()
    print(
        _table(
            ["SETTING", "VALUE"],
            # An unset optional setting is `None` in the payload (JSON null);
            # the table shows it as a word rather than an empty cell.
            [
                [name, "unset" if value is None else value]
                for name, value in payload["settings"].items()
            ],
        )
    )
    return 0


def _info_payload(settings: Settings) -> dict[str, Any]:
    import researcher_python

    rendered: dict[str, Any] = {
        # Email is printable when set, but absent rather than "" when not, so
        # `info --format json` emits null instead of an empty string.
        "email": settings.email or None,
        "output_dir": str(settings.output_dir),
        "log_level": settings.log_level,
        "max_results": settings.max_results,
        "timeout": settings.timeout,
        "retries": settings.retries,
    }
    for field in _KEY_FIELDS:
        # Never the value — only whether it is configured.
        rendered[field] = _set_unset(getattr(settings, field))

    return {
        "version": __version__,
        "package": researcher_python.__file__ or "",
        "python": sys.version.split()[0],
        "settings": rendered,
    }


# ---------------------------------------------------------------------- #
# sources
# ---------------------------------------------------------------------- #


def cmd_sources(args: argparse.Namespace, settings: Settings) -> int:
    """List every registered source with its capabilities and readiness."""
    entries = [_describe(name, settings) for name in source_names()]

    if args.format == "json":
        print(json.dumps(entries, indent=2, ensure_ascii=False, sort_keys=False))
        return 0

    rows = [
        [
            entry["name"],
            "yes" if entry["default"] else "no",
            _caps_string(entry["capabilities"]),
            entry["status"],
        ]
        for entry in entries
    ]
    print(_table(["NAME", "DEFAULT", "CAPS", "STATUS"], rows))
    return 0


def _describe(name: str, settings: Settings) -> dict[str, Any]:
    return {
        "name": name,
        "default": name in DEFAULT_SOURCES,
        "capabilities": _capabilities(name),
        "status": _status(name, settings),
    }


def _capabilities(name: str) -> dict[str, bool]:
    """Read the capability flags off the class — never constructs the source."""
    try:
        caps = get_source_class(name).capabilities
    except ResearcherError:
        return {field: False for field, _ in _CAP_FIELDS}
    return {field: bool(getattr(caps, field, False)) for field, _ in _CAP_FIELDS}


def _caps_string(capabilities: dict[str, bool]) -> str:
    return " ".join(
        letter if capabilities.get(field, False) else "-"
        for field, letter in _CAP_FIELDS
    )


def _status(name: str, settings: Settings) -> str:
    """Construct the adapter (no network) and classify what happens."""
    try:
        create_source(name, settings)
    except ConfigurationError as exc:
        env = _env_var_from_error(exc)
        return f"needs config ({env})" if env else "needs config"
    except SourceUnavailableError as exc:
        return f"unavailable ({exc.reason})" if exc.reason else "unavailable"
    except ResearcherError as exc:
        return f"error ({exc})"
    return "ready"


# ---------------------------------------------------------------------- #
# search
# ---------------------------------------------------------------------- #


def cmd_search(args: argparse.Namespace, settings: Settings) -> int:
    """Fan ``query`` across sources and print one merged, deduped result set."""
    if args.out and args.format == "table":
        # --out can only hold JSON/JSONL; a table is a terminal-only view.
        raise ConfigurationError(
            "--out writes JSON or JSONL; add --format json or --format jsonl"
        )

    requested = _requested_sources(args.sources)
    result = pipeline.search(
        args.query,
        sources=args.sources,
        max_results=args.max,
        sort=args.sort,
        settings=settings,
    )

    if args.format == "json":
        meta = {
            "query": args.query,
            "sources": requested,
            "sort": args.sort,
            "errors": {name: str(err) for name, err in result.errors.items()},
        }
        export.to_json(result.papers, args.out or sys.stdout, meta=meta)
    elif args.format == "jsonl":
        export.to_jsonl(result.papers, args.out or sys.stdout)
    else:
        print(_search_table(result.papers))

    # Diagnostics: one line per failed source, then the summary, on stderr.
    for name, error in result.errors.items():
        print(f"{name}: {type(error).__name__}: {_line(error)}", file=sys.stderr)

    attempted = list(result.by_source)
    failed = [name for name in attempted if name in result.errors]
    papers, sources_ok = len(result.papers), len(attempted) - len(failed)
    print(
        f"{papers} {'paper' if papers == 1 else 'papers'} from "
        f"{sources_ok} {'source' if sources_ok == 1 else 'sources'} "
        f"(failed: {', '.join(failed) if failed else 'none'})",
        file=sys.stderr,
    )
    # 0 whenever at least one source answered, even if it found nothing.
    return 1 if attempted and len(failed) == len(attempted) else 0


def _requested_sources(sources: list[str] | None) -> list[str]:
    if not sources:
        return list(DEFAULT_SOURCES)
    resolved: list[str] = []
    for name in sources:
        if name not in resolved:
            resolved.append(name)
    return resolved


def _first_author(paper: Paper) -> str:
    if not paper.authors:
        return ""
    name = paper.authors[0].name
    return f"{name} et al." if len(paper.authors) > 1 else name


def _truncate(text: str, width: int) -> str:
    text = _line(text)
    if len(text) <= width:
        return text
    return text[: width - 3].rstrip() + "..."


def _best_id(paper: Paper) -> str:
    return paper.doi or paper.arxiv_id or paper.pmid or paper.pmcid


def _search_table(papers: list[Paper]) -> str:
    rows = [
        [
            "" if paper.year is None else str(paper.year),
            _first_author(paper),
            _truncate(paper.title, 70),
            ", ".join(paper.sources),
            _best_id(paper),
        ]
        for paper in papers
    ]
    return _table(["YEAR", "AUTHOR", "TITLE", "SOURCES", "ID"], rows)


# ---------------------------------------------------------------------- #
# get
# ---------------------------------------------------------------------- #


def cmd_get(args: argparse.Namespace, settings: Settings) -> int:
    """Resolve one identifier and print a detail view (or its ``to_dict()``)."""
    paper = pipeline.get(args.identifier, source=args.source, settings=settings)
    if args.format == "json":
        print(json.dumps(paper.to_dict(), indent=2, ensure_ascii=False, sort_keys=False))
    else:
        print(_detail_view(paper))
    return 0


def _detail_view(paper: Paper) -> str:
    rows: list[list[Any]] = [
        ["title", paper.title],
        ["authors", ", ".join(author.name for author in paper.authors)],
        ["year", "" if paper.year is None else str(paper.year)],
        ["venue", paper.venue],
        ["url", paper.url],
        ["pdf_url", paper.pdf_url],
        [
            "citations",
            "" if paper.citation_count is None else str(paper.citation_count),
        ],
    ]
    # One line per identifier, so nothing is hidden behind a collapsed tuple.
    for key, value in paper.ids.items():
        rows.append(["ids", f"{key}: {value}"])

    out = _table(["FIELD", "VALUE"], rows)
    if paper.abstract:
        out += "\n\nabstract\n" + textwrap.fill(_line(paper.abstract), width=88)
    return out


# ---------------------------------------------------------------------- #
# download
# ---------------------------------------------------------------------- #


def cmd_download(args: argparse.Namespace, settings: Settings) -> int:
    """Fetch a PDF per identifier; keep going after individual failures."""
    failures = 0
    for identifier in args.identifiers:
        try:
            paper = pipeline.get(identifier, source=args.source, settings=settings)
            result = download.download_pdf(
                paper,
                args.output_dir,
                overwrite=args.overwrite,
                settings=settings,
            )
        except ConfigurationError:
            # Affects every identifier (e.g. no Unpaywall email) — stop at once.
            raise
        except ResearcherError as exc:
            failures += 1
            print(
                f"{identifier}: {type(exc).__name__}: {_line(exc)}", file=sys.stderr
            )
            continue

        print(result.path)  # stdout: one saved path per line
        print(f"{identifier}: via {result.via}", file=sys.stderr)
    return 1 if failures else 0
