"""Search a structured web source (Stack Overflow by default) and print rows,
or render the hits as citable bibliography entries.

Live wrapper around :func:`researcher_python.web_search`, the same shape as
``research.py``: data on stdout, summary on stderr, exit 0/1/2.

    python scripts/web_search.py "merge dicts" --max 5 --sort votes
    python scripts/web_search.py "merge dicts" --format bibtex --out so.bib
    python scripts/web_search.py "vim" --site serverfault --max 3

Run through the recipe so the dev shell is used: `just web-search ...`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys

import researcher_python as rp
from researcher_python.errors import ConfigurationError, ResearcherError

WEB_FORMATS = ("table", "bibtex", "ris", "csl")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="web_search",
        description="Search a structured web source (default: Stack Overflow).",
    )
    parser.add_argument("query", help="search text (title/intitle match)")
    parser.add_argument(
        "--source",
        default="stackoverflow",
        help="web source key (default: stackoverflow)",
    )
    parser.add_argument(
        "--site",
        default="stackoverflow",
        help="Stack Exchange site name (default: stackoverflow)",
    )
    parser.add_argument("--max", type=int, default=5, help="max records (default 5)")
    parser.add_argument(
        "--sort",
        default="relevance",
        help="sort order (default relevance; the source validates the value, "
        "e.g. relevance|activity|votes|creation for stackoverflow, "
        "relevance|hot|top|new|comments for reddit)",
    )
    parser.add_argument(
        "--subreddit",
        default="",
        help="reddit only: scope the search to one subreddit (r/ prefix optional)",
    )
    parser.add_argument(
        "--time",
        default="",
        choices=("", "hour", "day", "week", "month", "year", "all"),
        help="reddit only: time window t= (...|all)",
    )
    parser.add_argument(
        "--format",
        choices=WEB_FORMATS,
        default="table",
        help="output format (default table; bibtex/ris/csl cite the hits)",
    )
    parser.add_argument(
        "--out",
        metavar="FILE",
        default=None,
        help="write the citation output to FILE (implied by non-table formats)",
    )
    return parser


def _row(record: rp.WebResult) -> str:
    when = _dt.datetime.fromtimestamp(record.created or 0).strftime("%Y-%m-%d")
    score = "" if record.score is None else str(record.score)
    tags = " ".join(f"#{t}" for t in record.tags)
    return f"{score:>5}  {when}  {tags:<24}  {record.title}  {record.url}"


_RENDERERS = {
    "bibtex": rp.render_web_bibtex,
    "ris": rp.render_web_ris,
    "csl": rp.render_web_csl_json,
}
_WRITERS = {
    "bibtex": rp.web_to_bibtex,
    "ris": rp.web_to_ris,
    "csl": rp.web_to_csl_json,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # Default web_search() uses bare Settings(); only load_settings()
        # reads .env / the environment (keys, quota tier).
        settings = rp.load_settings()
        # Per-source keywords: --site for Stack Exchange sites, --subreddit
        # and --time for Reddit.  Pass only what the selected source accepts.
        source_kwargs: dict[str, object] = {}
        if args.source == "stackoverflow":
            source_kwargs["site"] = args.site
        elif args.source == "reddit":
            if args.subreddit:
                source_kwargs["subreddit"] = args.subreddit
            if args.time:
                source_kwargs["time_range"] = args.time
        result = rp.web_search(
            args.query,
            source=args.source,
            max_results=args.max,
            sort=args.sort,
            settings=settings,
            **source_kwargs,
        )
    except ConfigurationError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 2
    except ResearcherError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1

    if args.format == "table" or not result.results:
        if result.results:
            print(f"{'score':>5}  {'date':<10}  {'tags':<24}  title  url")
            for record in result.results:
                print(_row(record))
    elif args.out:
        _WRITERS[args.format](result.results, args.out)
        print(f"wrote {args.out} ({len(result.results)} records)", file=sys.stderr)
    else:
        sys.stdout.write(_RENDERERS[args.format](result.results))

    quota = result.quota or {}
    remaining = quota.get("remaining")
    total = quota.get("max")
    note = ""
    if remaining is not None:
        note = f" ({remaining}/{total or '?'} quota left)" if total else f" ({remaining} quota left)"
    print(
        f"{len(result.results)} record(s) via {result.source} "
        f"in {result.pages} page(s), truncated={result.truncated}{note}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())