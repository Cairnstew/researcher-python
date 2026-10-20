"""Regression tests for the gold-set recall benchmark.

The fixture is a small, committed slice of the real CW1 corpus: six queries
that were actually run, 26 papers a human labelled ``core``, and hard-negative
``excluded`` records sampled from the same pool.  The numbers pinned here are
the benchmark's contract — if a change to the scoring formula or to a floor
silently lowers recall, this test fails instead of the loss appearing only in
a future literature review.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from researcher_python import recall
from researcher_python.models import Paper

FIXTURE = Path(__file__).parent / "fixtures" / "recall_benchmark.json"
SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_search.py"


def _load() -> tuple[list[Paper], list[Paper], list[recall.QuerySpec]]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    papers = [Paper.from_dict(entry) for entry in payload["papers"]]
    gold = [paper for paper in papers if paper.label == "core"]
    queries = [
        recall.QuerySpec(entry["query"], entry.get("field", "any"))
        for entry in payload["queries"]
    ]
    return papers, gold, queries


def _metrics(config: recall.RecallConfig) -> dict:
    pool, gold, queries = _load()
    return recall.evaluate_config(pool, gold, queries, config)


class TestFixture:
    def test_fixture_is_small_and_labelled(self) -> None:
        pool, gold, queries = _load()
        assert 0 < len(pool) <= 100
        assert len(gold) == 26
        assert len(queries) == 6
        assert all(paper.label in {"core", "excluded"} for paper in pool)

    def test_every_gold_paper_is_in_the_pool(self) -> None:
        pool, gold, queries = _load()
        index = recall.gold_index(gold)
        matched = {recall.match_gold(paper, index) for paper in pool}
        assert {position for position in matched if position is not None} == set(
            range(len(gold))
        )

    def test_title_scopes_exclude_records_the_query_could_not_return(self) -> None:
        pool, gold, queries = _load()
        report = recall.evaluate_config(
            pool, gold, queries, recall.RecallConfig("scoped")
        )
        title_rows = [row for row in report["per_query"] if row["field"] == "title"]
        assert title_rows
        assert all(row["candidates"] < len(pool) for row in title_rows)


class TestRecallFloor:
    """The no-floor baseline and the floors that were safe on the real corpus."""

    def test_baseline_recall_cannot_drop(self) -> None:
        report = _metrics(recall.RecallConfig("baseline"))
        # Pinned from the committed fixture.  A lower number means a scoring
        # change lost a paper the benchmark previously surfaced.
        assert report["recall_at_5"] >= 3
        assert report["recall_at_10"] >= 24
        assert report["recall_at_20"] >= 26
        assert report["coverage"] >= 26
        assert report["gold_total"] == 26
        # Ranking quality, not just presence: a scoring change that keeps
        # coverage but sinks the gold papers fails here too.
        assert report["mrr"] >= 0.1497
        assert report["median_best_rank"] <= 8.0
        assert report["p90_best_rank"] <= 10
        assert report["purity"] >= 0.43

    def test_safe_absolute_floor_keeps_recall_and_lifts_purity(self) -> None:
        baseline = _metrics(recall.RecallConfig("baseline"))
        floored = _metrics(recall.RecallConfig("min20", min_score=0.20))
        assert floored["recall_at_20"] >= baseline["recall_at_20"]
        assert floored["coverage"] >= baseline["coverage"]
        assert floored["purity"] > baseline["purity"]

    def test_relative_floor_is_reported_with_its_recall_cost(self) -> None:
        # This is the trade-off the benchmark exists to expose: top_ratio=0.6
        # is the first setting in the grid that loses a gold paper here, so a
        # test that silently "fixed" it by loosening the fixture would hide a
        # real change in behaviour.
        strict = _metrics(recall.RecallConfig("top60", top_ratio=0.60))
        assert strict["coverage"] == 25
        assert strict["recall_at_20"] == 25

    def test_title_only_scoring_is_measured_not_assumed(self) -> None:
        scoped = _metrics(recall.RecallConfig("scoped"))
        title = _metrics(recall.RecallConfig("title", fields="title"))
        assert title["recall_at_20"] <= scoped["recall_at_20"]
        assert title["recall_at_5"] <= scoped["recall_at_5"]


class TestDeterminism:
    def test_same_fixture_yields_the_same_report(self) -> None:
        first = _metrics(recall.RecallConfig("baseline"))
        second = _metrics(recall.RecallConfig("baseline"))
        for key in (
            "kept_total",
            "gold_occurrences",
            "purity",
            "coverage",
            "mrr",
            "recall_at_5",
            "recall_at_10",
            "recall_at_20",
            "median_best_rank",
            "p90_best_rank",
        ):
            assert first[key] == second[key]


class TestConfigValidation:
    def test_bad_min_score_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_score"):
            recall.RecallConfig("bad", min_score=1.5)

    def test_bad_top_ratio_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="top_ratio"):
            recall.RecallConfig("bad", top_ratio=0.0)

    def test_bad_fields_mode_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="fields"):
            recall.RecallConfig("bad", fields="abstract")


class TestCommandLine:
    def test_fixture_run_writes_a_report(self, tmp_path: Path) -> None:
        target = tmp_path / "recall.json"
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--fixture", str(FIXTURE), "--out", str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        report = json.loads(target.read_text(encoding="utf-8"))
        assert report["gold_total"] == 26
        assert report["configs"]
        assert "scoped: none" in {row["name"] for row in report["configs"]}

    def test_fail_on_drop_exits_2_when_recall_regresses(self, tmp_path: Path) -> None:
        baseline = tmp_path / "baseline.json"
        # A baseline claiming every gold paper was surfaced is impossible to
        # match for the configurations that trade recall for purity, so the
        # gate must refuse the run rather than pass silently.
        baseline.write_text(
            json.dumps(
                {
                    "gold_total": 26,
                    "k_values": [5, 10, 20],
                    "configs": [
                        {
                            "name": "scoped: top 0.60",
                            "coverage": 26,
                            "recall_at_20": 26,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--fixture",
                str(FIXTURE),
                "--baseline",
                str(baseline),
                "--fail-on-drop",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 2, completed.stderr
        assert "recall regressions" in completed.stderr


class TestWeightingVariants:
    """The benchmark can compare the score's weights, not just its floors."""

    def test_coverage_only_does_not_lose_recall_and_improves_the_top_ranks(self) -> None:
        baseline = _metrics(recall.RecallConfig("baseline"))
        coverage = _metrics(
            recall.RecallConfig("coverage", coverage_weight=1.0)
        )
        assert coverage["coverage"] >= baseline["coverage"]
        assert coverage["recall_at_20"] >= baseline["recall_at_20"]
        assert coverage["recall_at_5"] >= baseline["recall_at_5"]

    def test_title_heavy_weighting_keeps_coverage(self) -> None:
        baseline = _metrics(recall.RecallConfig("baseline"))
        title_heavy = _metrics(
            recall.RecallConfig(
                "title-heavy",
                field_weights=(
                    ("title", 5.0),
                    ("keywords", 1.5),
                    ("categories", 1.0),
                    ("abstract", 1.0),
                    ("venue", 0.5),
                ),
            )
        )
        assert title_heavy["coverage"] >= baseline["coverage"]

    def test_unknown_field_weight_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown field"):
            recall.RecallConfig("bad", field_weights=(("not_a_field", 1.0),))

    def test_bad_coverage_weight_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="coverage_weight"):
            recall.RecallConfig("bad", coverage_weight=2.0)

    def test_coverage_only_with_a_floor_is_the_measured_sweet_spot(self) -> None:
        baseline = _metrics(recall.RecallConfig("baseline"))
        tuned = _metrics(
            recall.RecallConfig(
                "tuned", coverage_weight=1.0, min_score=0.30
            )
        )
        assert tuned["coverage"] >= baseline["coverage"]
        assert tuned["recall_at_20"] >= baseline["recall_at_20"]
        assert tuned["recall_at_5"] > baseline["recall_at_5"]
        assert tuned["mrr"] > baseline["mrr"]
        assert tuned["purity"] > baseline["purity"]
