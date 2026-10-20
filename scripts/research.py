"""One real research run through the whole pipeline.

End-to-end, against the live APIs (the offline suite never touches the
network; this script is the opposite — it exists to be run):

    search -> top record -> optional references fan-out -> optional PDF
                          -> BibTeX / RIS / CSL-JSON bibliography

Everything writes into ``--out`` (default ``research-out/``):
``papers.bib`` / ``papers.ris`` / ``papers.json`` (CSL-JSON) for the search
hits, plus ``references.bib`` / ``references.json`` when ``--refs`` is given.
Rate limits are the adapters' own gates; per-source failures (a throttled
Semantic Scholar, say) are reported and never abort the run, exactly as the
pipeline promises.

Exit codes: ``0`` success (even with isolated source errors), ``1`` no usable
results, ``2`` usage/configuration error.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import researcher_python as rp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research",
        description=(
            "Search the live APIs, then write a bibliography "
            "(BibTeX / RIS / CSL-JSON) into --out."
        ),
    )
    parser.add_argument(
        "query",
        nargs="?",
        default="attention is all you need",
        help="free-text query (default: %(default)s)",
    )
    parser.add_argument(
        "--source", action="append", dest="sources", metavar="NAME", default=None,
        help="source to search (repeatable; default: every source)",
    )
    parser.add_argument("--max", type=int, default=5, metavar="N", help="per-source cap")
    parser.add_argument(
        "--sort",
        choices=list(rp.SORT_MODES),
        default="relevance",
        help="result order for the search (default: %(default)s)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("research-out"),
        help="output directory (default: %(default)s)",
    )
    parser.add_argument(
        "--refs",
        action="store_true",
        help="also pull the references of the top record via the citation fan-out",
    )
    parser.add_argument(
        "--refs-source",
        action="append",
        dest="refs_sources",
        metavar="NAME",
        default=None,
        help="source for the reference fan-out (repeatable; default: every capable source)",
    )
    parser.add_argument("--refs-limit", type=int, default=None, metavar="N",
                        help="per-source reference cap")
    parser.add_argument(
        "--order",
        choices=list(rp.CITATION_SORT_MODES),
        default="canonical",
        help="reference ordering (default: %(default)s — the deterministic order)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail the whole run if any reference source fails",
    )
    parser.add_argument(
        "--pdf",
        action="store_true",
        help="download the top record's PDF (via its pdf_url, then Unpaywall)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = rp.load_settings()

    print(
        f"query: {args.query!r}  sources: {args.sources or 'default'}  "
        f"max: {args.max}  sort: {args.sort}",
        file=sys.stderr,
    )

    result = rp.search(
        args.query,
        sources=args.sources,
        max_results=args.max,
        sort=args.sort,
        settings=settings,
    )
    for name, error in result.errors.items():
        print(f"search {name}: {type(error).__name__}: {error}", file=sys.stderr)
    if not result.papers:
        print("no results from any source", file=sys.stderr)
        return 1
    print(f"search: {len(result.papers)} record(s)", file=sys.stderr)
    _print_papers(result.papers)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rp.to_bibtex(result.papers, out / "papers.bib")
    rp.to_ris(result.papers, out / "papers.ris")
    rp.to_csl_json(result.papers, out / "papers.json")
    print(f"wrote {out / 'papers.bib'}, {out / 'papers.ris'}, {out / 'papers.json'}",
          file=sys.stderr)

    top = result.papers[0]
    if args.refs:
        try:
            refs = rp.references(
                top,
                sources=args.refs_sources,
                strict=args.strict,
                limit=args.refs_limit,
                order=args.order,
                settings=settings,
            )
        except rp.ResearcherError as exc:
            print(f"references fan-out failed: {exc}", file=sys.stderr)
            return 2 if isinstance(exc, rp.ConfigurationError) else 1
        for name, error in refs.errors.items():
            print(f"references {name}: {type(error).__name__}: {error}", file=sys.stderr)
        print(
            f"references: {len(refs.papers)} work(s) via "
            f"{', '.join(refs.sources) or 'no sources'}"
            f"{'  [strict]' if refs.strict else ''}",
            file=sys.stderr,
        )
        for key, sources in refs.edges.items():
            print(f"  seen in {key}: {', '.join(sources)}", file=sys.stderr)
        for name, truncated in refs.truncated.items():
            if truncated:
                print(f"  {name}: may be truncated at {refs.requested}", file=sys.stderr)
        if refs.papers:
            rp.to_bibtex(refs.papers, out / "references.bib")
            rp.to_csl_json(refs.papers, out / "references.json")
            print(
                f"wrote {out / 'references.bib'}, {out / 'references.json'} "
                f"({len(refs.papers)} records)",
                file=sys.stderr,
            )

    if args.pdf:
        try:
            saved = rp.download_pdf(top, out / "pdf", settings=settings)
            print(f"downloaded {saved.path} via {saved.via}", file=sys.stderr)
        except rp.NoPdfError as exc:
            print(f"no open PDF for the top record: {exc}", file=sys.stderr)

    return 0


def _print_papers(papers: Sequence[rp.Paper], limit: int = 15) -> None:
    for index, paper in enumerate(papers[:limit], start=1):
        authors = paper.authors[0].name if paper.authors else ""
        if len(paper.authors) > 1:
            authors += " et al."
        print(f"{index:>2}. [{paper.year or '----'}] {paper.title[:92]}")
        if authors:
            print(f"     {authors}")
        identifiers = paper.doi or paper.arxiv_id or paper.pmid or paper.url
        print(f"     {identifiers}  ({', '.join(paper.sources)})")
    if len(papers) > limit:
        print(f"     … {len(papers) - limit} more", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))