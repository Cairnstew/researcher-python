#!/usr/bin/env python3
"""Offline dedupe/identity benchmark for a saved result set.

This measures whether the *merge* preserves the gold papers.  For query-level
recall — how many gold papers a relevance score and ``min_score``/``top_ratio``
configuration surfaces, and at what rank — use ``scripts/benchmark_search.py``.

The CW1 corpus is 1,077 screened records with a hand-screened 103-paper core.
Feature 1 (type filter), Feature 2 (citation backfill) and Feature 3 (version
linking) all change what a merged set looks like, so this script records the
same measurements before and after:

* how many input records survive ``dedupe``;
* how many duplicate title groups remain;
* whether the 103 golden papers are still recallable after the merge;
* citation/abstract coverage (the input for ``citations_per_year``);
* the work-type distribution (so a default exclusion is visible).

It makes no network calls: enrichment is measured separately by the pipeline's
own live check.  Run it before and after a change and diff the JSON:

    python scripts/benchmark_recall.py \
        --corpus  "/path/cw1-round3-screened-1077.json" \
        --golden  "/path/cw1-core-merged-103.json" \
        --out research-out/benchmark-baseline.json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from researcher_python import export
from researcher_python.ids import normalize_title
from researcher_python.models import Paper, _version_kind, dedupe
from researcher_python.sources.base import SearchFilters


def _identity(paper: Paper) -> set[str]:
    """Every key that could name this work in the merged set."""
    keys: set[str] = set()
    for key in paper.merge_keys():
        keys.add(key)
    title = paper.title_key()
    if title:
        keys.add(f"title:{title}")
    return keys


def _match_golden(golden: list[Paper], merged: list[Paper]) -> tuple[int, list[str]]:
    index: set[str] = set()
    for paper in merged:
        index.update(_identity(paper))
    missing: list[str] = []
    for paper in golden:
        keys = _identity(paper)
        if keys and not (keys & index):
            missing.append(paper.primary_id() or paper.title)
    return len(golden) - len(missing), missing


def _duplicate_titles(papers: list[Paper]) -> list[dict[str, Any]]:
    groups: dict[str, list[Paper]] = defaultdict(list)
    for paper in papers:
        key = normalize_title(paper.title)
        if key:
            groups[key].append(paper)
    out = []
    for key, items in groups.items():
        if len(items) > 1:
            out.append(
                {
                    "title": key,
                    "count": len(items),
                    "ids": [p.primary_id() for p in items],
                    "types": [p.work_type for p in items],
                    "years": [p.year for p in items],
                }
            )
    out.sort(key=lambda row: (-row["count"], row["title"]))
    return out


def _coverage(papers: list[Paper]) -> dict[str, float]:
    total = len(papers) or 1
    return {
        "citation": sum(p.citation_count is not None for p in papers) / total,
        "abstract": sum(bool(p.abstract.strip()) for p in papers) / total,
        "work_type": sum(bool(p.work_type.strip()) for p in papers) / total,
        "doi": sum(bool(p.doi) for p in papers) / total,
        "arxiv": sum(bool(p.arxiv_id) for p in papers) / total,
    }


def _type_filter(papers: list[Paper]) -> dict[str, Any]:
    filters = SearchFilters()
    kept = [p for p in papers if filters.type_ok(p.work_type)]
    removed = [p for p in papers if not filters.type_ok(p.work_type)]
    return {
        "exclude_types": list(filters.exclude_types or ()),
        "removed": len(removed),
        "kept": len(kept),
        "removed_by_type": dict(
            sorted(Counter(p.work_type or "(unknown)" for p in removed).items())
        ),
    }


def measure(corpus: list[Paper], golden: list[Paper] | None = None) -> dict[str, Any]:
    merged = dedupe(corpus)
    report: dict[str, Any] = {
        "input": len(corpus),
        "deduped": len(merged),
        "duplicates_collapsed": len(corpus) - len(merged),
        "duplicate_title_groups": len(_duplicate_titles(merged)),
        "duplicate_titles": _duplicate_titles(merged),
        "cross_version_title_groups": sum(
            1
            for row in _duplicate_titles(merged)
            if "preprint" in {_version_kind(p) for p in merged if normalize_title(p.title) == row["title"]}
            and "published"
            in {_version_kind(p) for p in merged if normalize_title(p.title) == row["title"]}
        ),
        "version_records": sum(1 for p in merged if len(p.versions) > 1),
        "explicit_version_entries": sum(len(p.versions) for p in merged),
        "citation_count_sources": dict(
            sorted(Counter(p.citation_count_source or "(unknown)" for p in merged).items())
        ),
        "coverage": _coverage(merged),
        "type_filter": _type_filter(merged),
        "work_types": dict(
            sorted(
                Counter(p.work_type or "(unknown)" for p in merged).items(),
                key=lambda item: (-item[1], item[0]),
            )
        ),
    }
    if golden is not None:
        matched, missing = _match_golden(golden, merged)
        report["golden_total"] = len(golden)
        report["golden_matched"] = matched
        report["golden_recall"] = matched / (len(golden) or 1)
        report["golden_missing"] = missing
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        action="append",
        required=True,
        help="screened JSON envelope (repeatable; rounds are merged in order)",
    )
    parser.add_argument("--golden", default=None, help="optional core JSON envelope")
    parser.add_argument("--out", default=None, help="write the report as JSON")
    args = parser.parse_args(argv)

    corpus: list[Paper] = []
    for path in args.corpus:
        corpus.extend(export.from_json(path))
    golden = export.from_json(args.golden) if args.golden else None
    report = measure(corpus, golden)

    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
