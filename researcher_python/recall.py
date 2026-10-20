"""Recall benchmark for the relevance score and its floors.

:mod:`researcher_python.relevance` provides two knobs that are easy to tune by
feel and hard to tune honestly: the ranking signal itself, and the
``min_score``/``top_ratio`` floors applied after it.  A floor that looks tidy
on one query can quietly drop a paper a human already judged relevant.

This module turns a hand-screened gold set into evidence.  Given

* a **pool** — every record the original queries retrieved, and
* a **gold set** — the papers a human kept (the 103 merged core papers in the
  CW1 corpus), and
* the **queries** that produced the pool,

it replays each query against the pool, ranks the candidates by the local
score, applies a configuration's floors, and reports what survived and at what
rank.  The pool is fixed, so the benchmark isolates the scoring/filtering
configuration from source-side retrieval changes.

The metrics are deliberately union-based across queries, because a paper is
*recallable* if any of the reviewer's queries surfaces it:

``coverage``
    Unique gold papers present in at least one query's kept set.  This is the
    headline number: 103/103 means no configuration lost a paper.
``recall_at_k``
    Unique gold papers present in at least one query's top *k*.  Use it to ask
    "would a reader scanning the first page see this paper?".
``mrr``
    Mean reciprocal rank over **all** gold papers, using each paper's best rank
    across every query (0 when it is never surfaced).  A recall figure that
    stays flat while MRR falls means relevant papers are sinking, not
    disappearing.
``purity``
    Gold occurrences divided by all kept occurrences.  Recall alone rewards a
    floor that keeps everything, so this is the cost side of the trade.
``mean_best_rank`` / ``median_best_rank`` / ``p90_best_rank``
    The shape of the rank distribution, so a few lucky top hits cannot hide a
    long tail.

Every function here is pure with respect to its inputs' *identity* (it does
stamp ``Paper.relevance``, like the pipeline) and deterministic: ties keep the
pool's input order.  Callers that need the pool unchanged can pass copies.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import relevance
from .models import Paper
from .sources.base import SearchFilters, matches_filters

__all__ = [
    "DEFAULT_K_VALUES",
    "QuerySpec",
    "RecallConfig",
    "default_configs",
    "evaluate",
    "evaluate_config",
    "gold_index",
    "match_gold",
]

#: Cut-offs reported by default.  Five is "first page", ten is "shortlist",
#: twenty is "before a screener gives up"; they answer different questions, so
#: a single k would hide the shape of the ranking.
DEFAULT_K_VALUES: tuple[int, ...] = (5, 10, 20)


@dataclass(frozen=True, slots=True)
class QuerySpec:
    """One replayed query and the field scope it was issued with.

    ``field`` must be what the original search used (``any``/``title``/
    ``abstract``).  The scope is part of the query: replaying a title-scoped
    search as free text would measure a search the reviewer never ran.
    """

    query: str
    field: str = "any"

    def __post_init__(self) -> None:
        if self.field not in ("any", "title", "abstract"):
            raise ValueError(f"unknown query field {self.field!r}")


@dataclass(frozen=True, slots=True)
class RecallConfig:
    """One scoring/floor configuration to measure.

    ``fields`` decides how the relevance score is computed:

    ``scoped``
        Honour the query's own field (the pipeline's behaviour).  A title
        query scores on the title alone, so the score keeps separating records
        instead of saturating.
    ``all``
        Score every record on every field regardless of the query scope.  This
        is the pre-scoping behaviour and the useful control.
    ``title``
        Score on the title only for every query — the aggressive alternative.

    ``min_score`` and ``top_ratio`` are passed straight to
    :func:`researcher_python.relevance.filter_by_score`; ``None`` disables
    either floor.  A configuration with both ``None`` is the no-floor baseline.

    ``field_weights``, ``phrase_bonus`` and ``coverage_weight`` are the same
    experimental overrides accepted by :func:`relevance.score_paper`.  They
    let the benchmark compare weighting schemes (title-heavy, density-heavy,
    no phrase bonus) without changing production scoring.  ``None`` keeps the
    documented default; ``field_weights`` is a tuple of pairs so the dataclass
    stays hashable and comparable.
    """

    name: str
    min_score: float | None = None
    top_ratio: float | None = None
    fields: str = "scoped"
    field_weights: tuple[tuple[str, float], ...] | None = None
    phrase_bonus: float | None = None
    coverage_weight: float = 0.75

    def __post_init__(self) -> None:
        if self.fields not in ("scoped", "all", "title"):
            raise ValueError(f"unknown fields mode {self.fields!r}")
        if self.min_score is not None and not 0.0 <= self.min_score <= 1.0:
            raise ValueError(f"min_score must be in [0, 1], got {self.min_score!r}")
        if self.top_ratio is not None and not 0.0 < self.top_ratio <= 1.0:
            raise ValueError(f"top_ratio must be in (0, 1], got {self.top_ratio!r}")
        if not 0.0 <= self.coverage_weight <= 1.0:
            raise ValueError(
                "coverage_weight must be in [0, 1], "
                f"got {self.coverage_weight!r}"
            )
        if self.phrase_bonus is not None and self.phrase_bonus < 0.0:
            raise ValueError(
                f"phrase_bonus must be non-negative, got {self.phrase_bonus!r}"
            )
        if self.field_weights is not None:
            known = set(relevance.FIELD_WEIGHTS)
            for field, weight in self.field_weights:
                if field not in known:
                    raise ValueError(f"unknown field {field!r} in field_weights")
                if weight < 0.0:
                    raise ValueError(
                        f"field weight for {field!r} must be non-negative, "
                        f"got {weight!r}"
                    )


def _identity_keys(paper: Paper) -> tuple[str, ...]:
    """Stable keys for gold matching, in a deterministic priority order.

    Stable ids come first (a DOI is not a coincidence), then the folded title
    as a fallback for records no source gave an id.  Unlike the dedupe
    matcher's title rule this does **not** require matching years: the gold set
    is already a curated union, and a false title match would only make recall
    look better than it is, so the fixture test pins the resulting numbers.
    """
    keys = list(paper.merge_keys())
    title = paper.title_key()
    if title:
        keys.append(f"title:{title}")
    return tuple(keys)


def gold_index(gold: Iterable[Paper]) -> dict[str, int]:
    """Map every identity key in ``gold`` to that paper's index (first wins)."""
    index: dict[str, int] = {}
    for position, paper in enumerate(gold):
        for key in _identity_keys(paper):
            index.setdefault(key, position)
    return index


def match_gold(paper: Paper, index: dict[str, int]) -> int | None:
    """The gold index for ``paper``, or ``None`` when it is not gold."""
    for key in _identity_keys(paper):
        position = index.get(key)
        if position is not None:
            return position
    return None


def _scope_candidates(pool: Sequence[Paper], spec: QuerySpec) -> list[Paper]:
    """Records the original field-scoped query could have returned.

    The pool is a union from many queries, so it contains records another query
    found.  Replaying a title-scoped query over the whole union would credit
    records that search could never have returned; ``matches_filters`` applies
    the same all-terms-in-the-field rule the adapters use.  A free-text query
    has no such restriction (``any`` matches everything).
    """
    if spec.field == "any":
        return list(pool)
    filters = SearchFilters(field=spec.field)
    return [paper for paper in pool if matches_filters(paper, filters, spec.query)]


def _score_fields(spec: QuerySpec, mode: str) -> list[str] | None:
    """Which fields to score, mirroring :func:`pipeline._score_fields`."""
    if mode == "all":
        return None
    if mode == "title":
        return ["title"]
    if spec.field == "title":
        return ["title"]
    if spec.field == "abstract":
        return ["abstract", "title"]
    return None


def _ranked(candidates: Sequence[Paper], spec: QuerySpec, config: RecallConfig) -> list[Paper]:
    """Score candidates against ``spec`` and return them best-first.

    ``score_papers`` preserves input order, and Python's sort is stable, so
    equal scores keep the pool order — the benchmark is reproducible to the
    last rank, which is what lets the regression test pin a floor.
    """
    scored = relevance.score_papers(
        candidates,
        spec.query,
        _score_fields(spec, config.fields),
        field_weights=dict(config.field_weights) if config.field_weights else None,
        phrase_bonus=config.phrase_bonus,
        coverage_weight=config.coverage_weight,
    )
    return sorted(scored, key=lambda paper: -(paper.relevance or 0.0))


def _rank_summary(ranks: Sequence[int]) -> dict[str, float | int | None]:
    """Mean/median/p90 of a rank list, using ``None`` for an empty list."""
    if not ranks:
        return {"mean_best_rank": None, "median_best_rank": None, "p90_best_rank": None}
    ordered = sorted(ranks)
    p90_index = int(round(0.9 * (len(ordered) - 1)))
    return {
        "mean_best_rank": round(statistics.fmean(ordered), 2),
        "median_best_rank": round(statistics.median(ordered), 2),
        "p90_best_rank": ordered[p90_index],
    }


def evaluate_config(
    pool: Sequence[Paper],
    gold: Sequence[Paper],
    queries: Sequence[QuerySpec],
    config: RecallConfig,
    *,
    k_values: Sequence[int] = DEFAULT_K_VALUES,
) -> dict[str, Any]:
    """Measure one :class:`RecallConfig` against a gold set.

    The returned dict has a ``config`` block, the union metrics described in
    the module docstring, and one ``per_query`` row per replay so a regression
    can be traced back to the query that caused it.
    """
    ks = tuple(sorted(set(k_values)))
    index = gold_index(gold)
    gold_total = len(gold)
    # Building identity keys is the expensive part of matching, and the same
    # pool record is looked at by every query and every k.  Cache by object
    # identity; the pool is not mutated in a way that changes its ids.
    positions: dict[int, int | None] = {}

    def locate(paper: Paper) -> int | None:
        key = id(paper)
        if key not in positions:
            positions[key] = match_gold(paper, index)
        return positions[key]

    # Best rank each gold reached across all queries; missing => never surfaced.
    best_rank: dict[int, int] = {}
    top_hits: dict[int, set[int]] = {k: set() for k in ks}
    kept_occurrences = 0
    gold_occurrences = 0
    rows: list[dict[str, Any]] = []

    for spec in queries:
        candidates = _scope_candidates(pool, spec)
        ranked = _ranked(candidates, spec, config)
        kept, dropped = relevance.filter_by_score(
            ranked, config.min_score, config.top_ratio
        )

        in_scope: set[int] = set()
        in_kept: set[int] = set()
        first_gold_rank: int | None = None
        for rank, paper in enumerate(kept, start=1):
            position = locate(paper)
            if position is None:
                continue
            in_kept.add(position)
            if first_gold_rank is None:
                first_gold_rank = rank
            previous = best_rank.get(position)
            if previous is None or rank < previous:
                best_rank[position] = rank
        for paper in candidates:
            position = locate(paper)
            if position is not None:
                in_scope.add(position)
        for k in ks:
            for paper in kept[:k]:
                position = locate(paper)
                if position is not None:
                    top_hits[k].add(position)

        kept_occurrences += len(kept)
        gold_occurrences += len(in_kept)
        rows.append(
            {
                "query": spec.query,
                "field": spec.field,
                "candidates": len(candidates),
                "gold_in_scope": len(in_scope),
                "kept": len(kept),
                "dropped": dropped,
                "gold_kept": len(in_kept),
                "first_gold_rank": first_gold_rank,
                **{
                    f"gold_in_top_{k}": sum(
                        1
                        for paper in kept[:k]
                        if locate(paper) is not None
                    )
                    for k in ks
                },
            }
        )

    ranks = list(best_rank.values())
    coverage = len(ranks)
    metrics: dict[str, Any] = {
        "name": config.name,
        "config": {
            "min_score": config.min_score,
            "top_ratio": config.top_ratio,
            "fields": config.fields,
        },
        "gold_total": gold_total,
        "queries": len(queries),
        "kept_total": kept_occurrences,
        "gold_occurrences": gold_occurrences,
        "purity": round(gold_occurrences / kept_occurrences, 4)
        if kept_occurrences
        else 0.0,
        "coverage": coverage,
        "coverage_rate": round(coverage / gold_total, 4) if gold_total else 0.0,
        "mrr": round(
            sum(1.0 / rank for rank in ranks) / gold_total if gold_total else 0.0,
            4,
        ),
        **_rank_summary(ranks),
    }
    for k in ks:
        metrics[f"recall_at_{k}"] = len(top_hits[k])
        metrics[f"recall_at_{k}_rate"] = (
            round(len(top_hits[k]) / gold_total, 4) if gold_total else 0.0
        )
    metrics["per_query"] = rows
    return metrics


def default_configs() -> list[RecallConfig]:
    """A small grid covering the questions the benchmark exists to answer.

    It is deliberately not exhaustive: the no-floor control, each floor alone,
    the pair, and an all-fields control for the scoping change.  A wider sweep
    is cheap to build by hand with :class:`RecallConfig`.
    """
    configs = [
        RecallConfig("scoped: none", fields="scoped"),
        RecallConfig("scoped: min 0.20", min_score=0.20, fields="scoped"),
        RecallConfig("scoped: min 0.30", min_score=0.30, fields="scoped"),
        RecallConfig("scoped: min 0.40", min_score=0.40, fields="scoped"),
        RecallConfig("scoped: top 0.40", top_ratio=0.40, fields="scoped"),
        RecallConfig("scoped: top 0.50", top_ratio=0.50, fields="scoped"),
        RecallConfig("scoped: top 0.60", top_ratio=0.60, fields="scoped"),
        RecallConfig(
            "scoped: min 0.20 + top 0.50",
            min_score=0.20,
            top_ratio=0.50,
            fields="scoped",
        ),
        RecallConfig(
            "scoped: min 0.30 + top 0.50",
            min_score=0.30,
            top_ratio=0.50,
            fields="scoped",
        ),
        RecallConfig(
            "scoped: min 0.30 + top 0.60",
            min_score=0.30,
            top_ratio=0.60,
            fields="scoped",
        ),
        RecallConfig("all fields: none", fields="all"),
        RecallConfig(
            "all fields: min 0.30 + top 0.50",
            min_score=0.30,
            top_ratio=0.50,
            fields="all",
        ),
        RecallConfig(
            "title only: min 0.30 + top 0.50",
            min_score=0.30,
            top_ratio=0.50,
            fields="title",
        ),
        RecallConfig(
            "relevance: title-heavy",
            fields="scoped",
            field_weights=(
                ("title", 5.0),
                ("keywords", 1.5),
                ("categories", 1.0),
                ("abstract", 1.0),
                ("venue", 0.5),
            ),
        ),
        RecallConfig("relevance: coverage only", fields="scoped", coverage_weight=1.0),
        RecallConfig(
            "relevance: coverage only + min 0.30",
            fields="scoped",
            coverage_weight=1.0,
            min_score=0.30,
        ),
        RecallConfig(
            "relevance: coverage only + top 0.40",
            fields="scoped",
            coverage_weight=1.0,
            top_ratio=0.40,
        ),
        RecallConfig("relevance: density half", fields="scoped", coverage_weight=0.5),
    ]
    return configs


def evaluate(
    pool: Sequence[Paper],
    gold: Sequence[Paper],
    queries: Sequence[QuerySpec],
    configs: Sequence[RecallConfig] | None = None,
    *,
    k_values: Sequence[int] = DEFAULT_K_VALUES,
) -> dict[str, Any]:
    """Run every configuration and return a comparable JSON-ready report.

    Configurations are sorted by the largest reported ``recall_at_k`` (then
    coverage, then purity) so the first row is the safest high-recall choice,
    not merely the tidiest.  The caller still owns the trade-off: a lower row
    may be preferable when purity matters more than the last few gold papers.
    """
    chosen = list(configs) if configs is not None else default_configs()
    index = gold_index(gold)
    gold_in_pool = sum(
        1 for position in {match_gold(p, index) for p in pool} if position is not None
    )
    largest_k = max(k_values) if k_values else 0
    reports = [
        evaluate_config(pool, gold, queries, config, k_values=k_values)
        for config in chosen
    ]
    reports.sort(
        key=lambda report: (
            -report.get(f"recall_at_{largest_k}", 0),
            -report["coverage"],
            -report["purity"],
            report["name"],
        )
    )
    return {
        "gold_total": len(gold),
        "gold_in_pool": gold_in_pool,
        "query_count": len(queries),
        "k_values": sorted(set(k_values)),
        "configs": reports,
    }
