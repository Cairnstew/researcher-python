#!/usr/bin/env python3
"""Measure recall of the relevance score and its floors against a gold set.

Two ways to feed it:

**Real corpus** — a saved candidate pool, the hand-screened gold set, and the
search log that produced the pool::

    python scripts/benchmark_search.py \
        --pool  cw1-workspace/results.json \
        --gold  cw1-core-merged-103.json \
        --search-log cw1-workspace/search-log.jsonl \
        --out research-out/recall-baseline.json

**Fixture** — one self-contained JSON file with ``queries`` and ``papers``
(``label: "core"`` marks gold).  The regression test uses this shape::

    python scripts/benchmark_search.py \
        --fixture tests/fixtures/recall_benchmark.json

The pool is fixed, so this measures the *configuration* (score field scope,
``min_score``, ``top_ratio``), not source-side retrieval.  To benchmark a new
query set, produce a new pool with the normal search/workspace flow first.

Exit status is 0 on success.  With ``--fail-on-drop`` it is 2 when a metric in
``--baseline`` regresses, which is how CI can keep a scoring change honest.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from researcher_python import export, recall
from researcher_python.models import Paper


def _load_fixture(path: Path) -> tuple[list[Paper], list[Paper], list[recall.QuerySpec]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "papers" not in payload or "queries" not in payload:
        raise SystemExit(f"{path}: fixture needs 'queries' and 'papers'")
    papers = [Paper.from_dict(entry) for entry in payload["papers"]]
    gold = [paper for paper in papers if paper.label == "core"]
    queries = _parse_queries(payload["queries"], where=str(path))
    return papers, gold, queries


def _parse_queries(raw: Any, *, where: str) -> list[recall.QuerySpec]:
    if isinstance(raw, dict):
        raw = raw.get("queries")
    if not isinstance(raw, list):
        raise SystemExit(f"{where}: queries must be a list")
    queries: list[recall.QuerySpec] = []
    for entry in raw:
        if isinstance(entry, str):
            queries.append(recall.QuerySpec(entry))
        elif isinstance(entry, dict) and entry.get("query"):
            queries.append(
                recall.QuerySpec(str(entry["query"]), str(entry.get("field") or "any"))
            )
        else:
            raise SystemExit(f"{where}: bad query entry {entry!r}")
    # The same query+field may appear in several rounds/source combinations;
    # replaying it twice would double-count its kept records in `purity`.
    return list(dict.fromkeys(queries))


def _load_search_log(path: Path) -> list[recall.QuerySpec]:
    queries: list[recall.QuerySpec] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text:
            continue
        try:
            entry = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_number}: invalid JSON ({exc})") from exc
        params = entry.get("params") or {}
        query = str(params.get("query") or "").strip()
        if not query:
            continue
        queries.append(
            recall.QuerySpec(query, str(params.get("field") or "any"))
        )
    return list(dict.fromkeys(queries))


def _load_envelope(path: Path) -> list[Paper]:
    return export.from_json(path)


def _table(report: dict[str, Any]) -> str:
    ks = report["k_values"]
    headers = [
        "configuration",
        "kept",
        "purity",
        *[f"R@{k}" for k in ks],
        "coverage",
        "MRR",
        "median",
        "p90",
    ]
    rows = [headers, ["-" * len(h) for h in headers]]
    for config in report["configs"]:
        rows.append(
            [
                config["name"],
                str(config["kept_total"]),
                f"{config['purity']:.3f}",
                *[str(config[f"recall_at_{k}"]) for k in ks],
                f"{config['coverage']}/{report['gold_total']}",
                f"{config['mrr']:.3f}",
                str(config["median_best_rank"]),
                str(config["p90_best_rank"]),
            ]
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
    return "\n".join(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows
    )


def _compare(
    baseline: dict[str, Any], current: dict[str, Any], tolerance: float
) -> list[str]:
    """Return one line per metric that fell below the saved baseline."""
    previous = {row["name"]: row for row in baseline.get("configs", [])}
    ks = current.get("k_values") or []
    largest = max(ks) if ks else None
    regressions: list[str] = []
    for row in current.get("configs", []):
        old = previous.get(row["name"])
        if old is None:
            continue
        checks = [("coverage", "coverage")]
        if largest is not None:
            checks.append((f"recall_at_{largest}", f"recall_at_{largest}"))
        for key, label in checks:
            if key not in old:
                continue
            drop = old[key] - row.get(key, 0)
            if drop > tolerance:
                regressions.append(
                    f"{row['name']}: {label} {old[key]} -> {row.get(key, 0)} (-{drop})"
                )
    return regressions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, help="self-contained fixture JSON")
    parser.add_argument("--pool", type=Path, help="candidate-pool JSON envelope")
    parser.add_argument(
        "--gold",
        type=Path,
        help="gold-set JSON envelope (default: label=core records in --pool)",
    )
    parser.add_argument("--queries", type=Path, help="query list JSON")
    parser.add_argument(
        "--search-log", type=Path, help="workspace search-log.jsonl to replay"
    )
    parser.add_argument("--out", type=Path, help="write the full report as JSON")
    parser.add_argument(
        "--k",
        type=int,
        action="append",
        default=None,
        help="recall cut-off (repeatable; default 5, 10, 20)",
    )
    parser.add_argument(
        "--baseline", type=Path, help="previous report to compare against"
    )
    parser.add_argument(
        "--tolerance", type=float, default=0.0, help="allowed absolute metric drop"
    )
    parser.add_argument(
        "--fail-on-drop",
        action="store_true",
        help="exit 2 when coverage or the largest recall@k falls below --baseline",
    )
    args = parser.parse_args(argv)

    if args.fixture:
        pool, gold, queries = _load_fixture(args.fixture)
    else:
        if args.pool is None:
            parser.error("--pool required without --fixture")
        pool = _load_envelope(args.pool)
        # A workspace pool already carries screening labels, so `--gold` is
        # optional when the core set is simply `label == "core"` in the pool.
        gold = (
            _load_envelope(args.gold)
            if args.gold
            else [paper for paper in pool if paper.label == "core"]
        )
        if args.search_log:
            queries = _load_search_log(args.search_log)
        elif args.queries:
            queries = _parse_queries(
                json.loads(args.queries.read_text(encoding="utf-8")),
                where=str(args.queries),
            )
        else:
            parser.error("--queries or --search-log required without --fixture")

    if not queries:
        raise SystemExit("no queries to replay")
    if not gold:
        raise SystemExit("gold set is empty (fixture needs label: core)")

    k_values = tuple(sorted(set(args.k))) if args.k else recall.DEFAULT_K_VALUES
    report = recall.evaluate(pool, gold, queries, k_values=k_values)

    print(
        f"gold {report['gold_total']} | in pool {report['gold_in_pool']} | "
        f"queries {report['query_count']} | k {report['k_values']}"
    )
    print(_table(report))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        regressions = _compare(baseline, report, args.tolerance)
        if regressions:
            print("\nrecall regressions:", file=sys.stderr)
            for line in regressions:
                print(f"  {line}", file=sys.stderr)
            if args.fail_on_drop:
                return 2
        else:
            print("\nno recall regressions against the baseline")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
