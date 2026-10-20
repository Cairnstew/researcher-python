#!/usr/bin/env python3
"""Live verification for all five sources — a manual tool, not part of CI.

Run it by hand from an address that is not already rate-limited::

    nix develop --command python scripts/verify_live.py
    nix develop --command python scripts/verify_live.py \\
        --source crossref --source semantic_scholar
    nix develop --command python scripts/verify_live.py --dry-run

What it does: for each source it constructs the adapter and exercises every
capability that adapter actually declares — the check list is derived from
``source.capabilities``, never hardcoded — printing one row per (source,
check).  Each network call is preceded by a polite delay (2s by default,
``--delay`` to change).

Outcomes
    PASS      the check behaved as the contract says it should
    FAIL      it did not — the run exits 1
    SKIP      the check could not run (unsupported, needs config, no record)
    RATELIMIT the source answered 429 — its remaining checks are skipped
              rather than hammered, and the run exits 3

Exit codes: ``1`` if anything FAILed, else ``3`` if anything was RATELIMITED,
else ``0`` (SKIP never fails a run).  Usage/config errors exit ``2``.

The known identifiers below are deliberately constants so they are easy to
edit when an API changes; they are the only thing this script assumes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable

# Allow `python scripts/verify_live.py` from a plain checkout (nix develop
# sets PYTHONPATH, but the script should not depend on that).
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from researcher_python.config import Settings, load_settings  # noqa: E402
from researcher_python.errors import (  # noqa: E402
    ConfigurationError,
    NotFoundError,
    RateLimitError,
    SourceUnavailableError,
    UnsupportedOperation,
)
from researcher_python.ids import (  # noqa: E402
    is_arxiv_id,
    is_doi,
    normalize_arxiv_id,
    normalize_doi,
)
from researcher_python.models import Paper  # noqa: E402
from researcher_python.sources import (  # noqa: E402
    DEFAULT_SOURCES,
    create_source,
    get_source_class,
    source_names,
)

__all__ = ["build_parser", "exit_code", "main", "plan", "run"]

# ---------------------------------------------------------------------- #
# Known identifiers — edit these if an API stops recognising them.
# ---------------------------------------------------------------------- #

#: One identifier ``get()`` must resolve, per source.
KNOWN_IDS: dict[str, str] = {
    "arxiv": "1706.03762",
    "openalex": "10.1038/nature14539",
    "crossref": "10.1038/nature14539",
    # S2 404s on 10.1038/nature14539 (verified live 2026-10-06), so use the
    # NEJM record it does index — same DOI the pubmed check uses.
    "semantic_scholar": "10.1056/NEJMoa2034577",
    "pubmed": "10.1056/NEJMoa2034577",
}

#: An identifier of the *wrong* kind for that source; ``get()`` must say no.
WRONG_IDS: dict[str, str] = {
    "arxiv": "10.1038/nature14539",  # a DOI
    "crossref": "1706.03762",  # an arXiv id
    "openalex": "not-an-identifier",
    "semantic_scholar": "not-an-identifier",
    "pubmed": "not-an-identifier",
}

#: Crossref-only: mixed case on the wire, then lower-cased (D3).
MIXED_CASE_DOI = "10.1103/PhysRevLett.116.061102"
SEARCH_QUERY = "attention is all you need"
RESULT_CAP = 3

PASS, FAIL, SKIP, RATELIMIT = "PASS", "FAIL", "SKIP", "RATELIMIT"
_OUTCOMES = (PASS, FAIL, SKIP, RATELIMIT)
#: Checks that talk to the network (skipped wholesale after a rate limit).
_NETWORK = frozenset({"search", "get", "wrong-id", "references", "citations", "doi-case"})

_DETAIL_WIDTH = 60


@dataclass
class Check:
    source: str
    check: str
    outcome: str
    seconds: float
    detail: str


class Polite:
    """Sleep ``delay`` seconds *between* network calls, never before the first."""

    def __init__(self, delay: float) -> None:
        self.delay = max(0.0, float(delay))
        self._made = False

    def wait(self) -> None:
        if self._made and self.delay > 0:
            time.sleep(self.delay)
        self._made = True


# ---------------------------------------------------------------------- #
# Planning
# ---------------------------------------------------------------------- #


def plan(name: str, capabilities: Any) -> list[str]:
    """Which checks apply to ``name``, derived from its declared capabilities."""
    checks = ["construct", "search", "get", "wrong-id"]
    if capabilities.references:
        checks.append("references")
    # ``citations`` always gets a row: unsupported is reported as SKIP *and*
    # asserted to refuse cleanly with UnsupportedOperation.
    checks.append("citations")
    if capabilities.pdf:
        checks.append("pdf-url")
    if name == "crossref":
        checks.append("doi-case")
    return checks


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


def _brief(exc: BaseException) -> str:
    """``TypeName: first line of the message`` — never a traceback."""
    first = (str(exc).splitlines() or [""])[0]
    return f"{type(exc).__name__}: {first}" if first else type(exc).__name__


def _config_detail(exc: ConfigurationError) -> str:
    """Name the env var the error names, e.g. ``needs RESEARCHER_EMAIL``."""
    match = re.search(r"export (\S+) first", str(exc))
    return f"needs {match.group(1)}" if match else _brief(exc)


def _id_matches(paper: Paper, identifier: str) -> bool:
    """The returned record must actually be the one we asked for."""
    if is_doi(identifier):
        return bool(paper.doi) and normalize_doi(paper.doi) == normalize_doi(identifier)
    if is_arxiv_id(identifier):
        return bool(paper.arxiv_id) and normalize_arxiv_id(
            paper.arxiv_id
        ) == normalize_arxiv_id(identifier)
    return bool(paper.pmid) and paper.pmid == identifier


def _list_result(found: Any, noun: str) -> tuple[str, str, Paper | None]:
    if not isinstance(found, list):
        return FAIL, f"returned {type(found).__name__}, not a list", None
    return PASS, f"{len(found)} {noun}", None


# ---------------------------------------------------------------------- #
# Checks — each returns (outcome, detail, paper-or-None)
# ---------------------------------------------------------------------- #

Runner = Callable[[str, Any, Any, Paper | None, "Polite"], tuple[str, str, Any]]


def _check_search(name: str, source: Any, caps: Any, paper: Any, gate: Polite):
    gate.wait()
    # NB: Source.search takes `limit=`, not `max_results=` — the latter is
    # a Settings field, not a method kwarg.
    found = source.search(SEARCH_QUERY, limit=2)
    if not isinstance(found, list):
        return FAIL, f"returned {type(found).__name__}, not a list", None
    titled = [p for p in found if isinstance(p, Paper) and p.title]
    if not titled:
        return FAIL, f"{len(found)} papers, none with a title", None
    return PASS, f"{len(found)} papers", None


def _check_get(name: str, source: Any, caps: Any, paper: Any, gate: Polite):
    identifier = KNOWN_IDS[name]
    gate.wait()
    found = source.get(identifier)
    if not isinstance(found, Paper) or not found.title:
        return FAIL, "no title in the returned record", None
    if not _id_matches(found, identifier):
        return (
            FAIL,
            f"ids do not match: got {found.doi or found.arxiv_id or found.pmid}",
            found,
        )
    return PASS, found.title, found


def _check_wrong_id(name: str, source: Any, caps: Any, paper: Any, gate: Polite):
    gate.wait()
    try:
        source.get(WRONG_IDS[name])
    except RateLimitError:
        raise  # handled by the driver, not a wrong-id verdict
    except NotFoundError:
        return PASS, "raised NotFoundError", None
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        return FAIL, _brief(exc), None
    return FAIL, "returned a record instead of raising", None


def _check_references(name: str, source: Any, caps: Any, paper: Any, gate: Polite):
    if paper is None:
        return SKIP, "no record to resolve", None
    gate.wait()
    return _list_result(source.references(paper)[:RESULT_CAP], "references")


def _check_citations(name: str, source: Any, caps: Any, paper: Any, gate: Polite):
    if not caps.citations:
        # Declaring no capability must refuse cleanly rather than crash.
        try:
            source.citations(paper if paper is not None else Paper())
        except RateLimitError:
            raise
        except UnsupportedOperation:
            return SKIP, "unsupported", None
        except Exception as exc:  # noqa: BLE001
            return FAIL, _brief(exc), None
        return FAIL, "did not raise UnsupportedOperation", None
    if paper is None:
        return SKIP, "no record to resolve", None
    gate.wait()
    return _list_result(source.citations(paper)[:RESULT_CAP], "citations")


def _check_pdf_url(name: str, source: Any, caps: Any, paper: Any, gate: Polite):
    # A field read, not a request — deliberately no gate.wait().
    if paper is None:
        return SKIP, "no record to inspect", None
    if paper.pdf_url:
        return PASS, paper.pdf_url, None
    return SKIP, "no OA pdf", None


def _check_doi_case(name: str, source: Any, caps: Any, paper: Any, gate: Polite):
    """Mixed-case in, lower-case in — both must agree (D3)."""
    gate.wait()
    try:
        mixed = source.get(MIXED_CASE_DOI)
        lower = source.get(MIXED_CASE_DOI.lower())
    except RateLimitError:
        raise
    except Exception as exc:  # noqa: BLE001
        return FAIL, _brief(exc), None
    if not mixed.title or not lower.title:
        return FAIL, f"missing title: {mixed.title!r} vs {lower.title!r}", None
    if mixed.title != lower.title:
        return FAIL, f"{mixed.title!r} != {lower.title!r}", None
    return PASS, "case-insensitive", None


_RUNNERS: dict[str, Runner] = {
    "search": _check_search,
    "get": _check_get,
    "wrong-id": _check_wrong_id,
    "references": _check_references,
    "citations": _check_citations,
    "pdf-url": _check_pdf_url,
    "doi-case": _check_doi_case,
}


# ---------------------------------------------------------------------- #
# Driver
# ---------------------------------------------------------------------- #


def _construct(name: str, settings: Settings) -> tuple[Any, Check | None]:
    """Build the adapter; return ``(source, None)`` or ``(None, a SKIP/FAIL row)``."""
    try:
        return create_source(name, settings), None
    except ConfigurationError as exc:
        return None, Check(name, "construct", SKIP, 0.0, _config_detail(exc))
    except SourceUnavailableError as exc:
        return None, Check(name, "construct", SKIP, 0.0, exc.reason or "unavailable")
    except Exception as exc:  # noqa: BLE001 - never abort the run
        return None, Check(name, "construct", FAIL, 0.0, _brief(exc))


def _verify_source(name: str, settings: Settings, gate: Polite) -> list[Check]:
    source, failure = _construct(name, settings)
    if source is None:
        assert failure is not None
        return [failure]

    rows: list[Check] = [Check(name, "construct", PASS, 0.0, "built")]
    capabilities = source.capabilities
    rate_limited = False
    paper: Paper | None = None

    for check in plan(name, capabilities)[1:]:  # 'construct' is already done
        if rate_limited and check in _NETWORK:
            rows.append(Check(name, check, SKIP, 0.0, "after rate limit"))
            continue

        runner = _RUNNERS[check]
        start = time.monotonic()
        try:
            outcome, detail, found = runner(name, source, capabilities, paper, gate)
            seconds = round(time.monotonic() - start, 3)
        except RateLimitError as exc:
            outcome, seconds = RATELIMIT, round(time.monotonic() - start, 3)
            detail = f"retry_after={exc.retry_after} {_brief(exc)}"
            rate_limited = True
            found = None
        except Exception as exc:  # noqa: BLE001 - never abort the run
            outcome, seconds, detail, found = FAIL, 0.0, _brief(exc), None

        rows.append(Check(name, check, outcome, seconds, detail))
        if check == "get" and isinstance(found, Paper):
            paper = found
    return rows


def run(names: list[str], settings: Settings, delay: float = 2.0) -> list[Check]:
    """Verify every source in ``names``; one row per (source, check)."""
    gate = Polite(delay)
    rows: list[Check] = []
    for name in names:
        rows.extend(_verify_source(name, settings, gate))
    return rows


def exit_code(rows: list[Check]) -> int:
    """FAIL beats RATELIMIT beats success; SKIP never fails a run."""
    if any(row.outcome == FAIL for row in rows):
        return 1
    if any(row.outcome == RATELIMIT for row in rows):
        return 3
    return 0


def _resolve(names: list[str] | None) -> list[str]:
    if not names:
        return list(DEFAULT_SOURCES)
    known = source_names()
    for name in names:
        if name not in known:
            raise ConfigurationError(
                f"unknown source {name!r}; known sources: {', '.join(known)}"
            )
    resolved: list[str] = []
    for name in names:
        if name not in resolved:
            resolved.append(name)
    return resolved


# ---------------------------------------------------------------------- #
# Output
# ---------------------------------------------------------------------- #


def _summary(rows: list[Check]) -> str:
    counts: dict[str, int] = {outcome: 0 for outcome in _OUTCOMES}
    for row in rows:
        counts[row.outcome] = counts.get(row.outcome, 0) + 1
    return " ".join(f"{counts[outcome]} {outcome}" for outcome in _OUTCOMES)


def _render_table(header: list[str], body: list[list[str]]) -> str:
    widths = [
        max([len(h)] + [len(str(row[i])) for row in body]) for i, h in enumerate(header)
    ]

    def line(cells: list[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()

    out = [line(header), line(["-" * w for w in widths])]
    out.extend(line(row) for row in body)
    return "\n".join(out)


def _result_table(rows: list[Check]) -> str:
    return _render_table(
        ["SOURCE", "CHECK", "OUTCOME", "SECONDS", "DETAIL"],
        [
            [
                row.source,
                row.check,
                row.outcome,
                f"{row.seconds:.3f}",
                row.detail[:_DETAIL_WIDTH],
            ]
            for row in rows
        ],
    )


def _plan_rows(names: list[str]) -> list[tuple[str, str]]:
    return [
        (name, check)
        for name in names
        for check in plan(name, get_source_class(name).capabilities)
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_live.py",
        description=(
            "Exercise every declared capability of every source against the live "
            "APIs. Manual tool — not part of the offline test suite."
        ),
        epilog="exit codes: 0 ok, 1 any FAIL, 3 any RATELIMIT, 2 usage/config",
    )
    parser.add_argument(
        "--source",
        action="append",
        dest="sources",
        metavar="NAME",
        default=None,
        help="source to verify (repeatable; default: all five)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="polite gap between network calls (default: 2.0)",
    )
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="output format",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned checks per source and exit without calling anything",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="override settings.timeout for this run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        names = _resolve(args.sources)
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.dry_run:
        planned = _plan_rows(names)
        if args.format == "json":
            print(
                json.dumps(
                    [{"source": s, "check": c} for s, c in planned],
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(
                _render_table(
                    ["SOURCE", "CHECK"], [[s, c] for s, c in planned]
                )
            )
            print(f"{len(planned)} checks planned")
        return 0

    settings = load_settings()
    if args.timeout is not None:
        settings = replace(settings, timeout=args.timeout)

    rows = run(names, settings, delay=args.delay)
    if args.format == "json":
        print(json.dumps([asdict(row) for row in rows], indent=2, ensure_ascii=False))
    else:
        if rows:
            print(_result_table(rows))
        print(_summary(rows))
    return exit_code(rows)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
