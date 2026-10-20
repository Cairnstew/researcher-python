"""Tests for :mod:`researcher_python.relevance` and the ``score`` sort mode.

The behaviour these pin down is the reason the module exists: a merged result
set must be rankable and filterable on one shared yardstick, and it must be
*deterministic* — the same record and query always score the same, or a saved
result file stops being reproducible.
"""

from __future__ import annotations

import pytest

import researcher_python as rp
from researcher_python import matrix, relevance
from researcher_python.errors import NotFoundError
from researcher_python.models import Paper, sort_papers
from researcher_python.sources.base import Capabilities, Source


def _paper(**kwargs) -> Paper:
    base = {"sources": ["openalex"], "doi": "10.1/x"}
    base.update(kwargs)
    return Paper(**base)


ON_TOPIC = _paper(
    title="Legal liability for artificial intelligence in autonomous vehicles",
    abstract=(
        "This paper examines legal liability, regulation and mitigation for "
        "artificial intelligence systems deployed in transport."
    ),
)
OFF_TOPIC = _paper(
    title="Global burden of cardiovascular disease in 204 countries",
    abstract="We report estimates of the global burden of cardiovascular disease.",
)
PERIPHERAL = _paper(
    title="Regulation as a social practice",
    abstract="Regulation is discussed without reference to intelligent systems.",
)


class TestQueryTerms:
    def test_stopwords_and_single_chars_are_dropped(self) -> None:
        assert relevance.query_terms("What is the role of AI?") == ["role", "ai"]

    def test_order_is_preserved_and_deduplicated(self) -> None:
        assert relevance.query_terms("bias bias fairness") == ["bias", "fairness"]

    def test_stopword_only_query_yields_no_terms(self) -> None:
        assert relevance.query_terms("of the and") == []

    def test_punctuation_does_not_split_terms(self) -> None:
        assert relevance.query_terms("self-driving cars") == ["self", "driving", "cars"]


class TestScorePaper:
    def test_on_topic_beats_off_topic(self) -> None:
        query = "legal liability of artificial intelligence"
        terms = relevance.query_terms(query)
        on = relevance.score_paper(ON_TOPIC, terms)
        off = relevance.score_paper(OFF_TOPIC, terms)
        assert on > off
        assert off == 0.0

    def test_peripheral_record_scores_above_off_topic(self) -> None:
        terms = relevance.query_terms("AI regulation")
        assert relevance.score_paper(PERIPHERAL, terms) > relevance.score_paper(
            OFF_TOPIC, terms
        )

    def test_score_is_bounded(self) -> None:
        terms = relevance.query_terms("legal liability artificial intelligence")
        value = relevance.score_paper(ON_TOPIC, terms)
        assert 0.0 <= value <= 1.0

    def test_no_terms_scores_zero_rather_than_raising(self) -> None:
        assert relevance.score_paper(ON_TOPIC, []) == 0.0

    def test_stopword_only_query_scores_zero(self) -> None:
        assert relevance.score_paper(ON_TOPIC, relevance.query_terms("of the")) == 0.0

    def test_is_pure_and_deterministic(self) -> None:
        terms = relevance.query_terms("artificial intelligence liability")
        first = relevance.score_paper(ON_TOPIC, terms)
        second = relevance.score_paper(ON_TOPIC, terms)
        assert first == second

    def test_title_phrase_beats_scattered_matches(self) -> None:
        terms = relevance.query_terms("legal liability")
        phrase = _paper(
            title="Legal liability reform",
            abstract="An unrelated discussion of industrial design.",
        )
        scattered = _paper(
            title="Reform of liability in commerce",
            abstract="Legal scholarship that mentions liability and reform "
            "repeatedly without either in the title.",
        )
        assert relevance.score_paper(phrase, terms) > relevance.score_paper(
            scattered, terms
        )

    def test_empty_record_scores_zero(self) -> None:
        assert relevance.score_paper(_paper(), relevance.query_terms("ai")) == 0.0

    def test_substring_match_catches_inflections(self) -> None:
        terms = relevance.query_terms("bias")
        inflected = _paper(title="Biased models and dataset bias")
        assert relevance.score_paper(inflected, terms) > 0.0


class TestScorePapers:
    def test_stamps_relevance_on_every_record(self) -> None:
        papers = relevance.score_papers([ON_TOPIC, OFF_TOPIC], "legal liability")
        assert papers[0].relevance is not None
        assert papers[0].relevance > 0
        assert papers[1].relevance == 0.0

    def test_preserves_input_order(self) -> None:
        papers = relevance.score_papers([ON_TOPIC, OFF_TOPIC], "legal liability")
        assert [p.title for p in papers] == [ON_TOPIC.title, OFF_TOPIC.title]

    def test_works_through_the_public_api(self) -> None:
        papers = rp.score_papers([ON_TOPIC], "legal liability")
        assert papers[0].relevance is not None


class TestFilterByScore:
    def test_none_keeps_everything(self) -> None:
        kept, dropped = relevance.filter_by_score([ON_TOPIC, OFF_TOPIC], None)
        assert len(kept) == 2
        assert dropped == 0

    def test_floor_removes_low_scores_and_counts_them(self) -> None:
        scored = relevance.score_papers([ON_TOPIC, OFF_TOPIC], "legal liability")
        kept, dropped = relevance.filter_by_score(scored, 0.2)
        assert [p.title for p in kept] == [ON_TOPIC.title]
        assert dropped == 1

    def test_unscored_records_are_dropped_not_assumed(self) -> None:
        kept, dropped = relevance.filter_by_score([_paper()], 0.2)
        assert kept == []
        assert dropped == 1

    def test_threshold_is_inclusive(self) -> None:
        paper = _paper(title="liability liability liability")
        paper.relevance = 0.5
        kept, _ = relevance.filter_by_score([paper], 0.5)
        assert len(kept) == 1


class TestTopRatioFloor:
    """The relative floor: scale-free, so it cuts the tail on any query length."""

    def _ladder(self):
        """Five records on a 1.0 / 0.6 / 0.55 / 0.5 / 0.2 score ladder."""
        out = []
        for doi, score in (
            ("10.1/a", 1.0),
            ("10.1/b", 0.6),
            ("10.1/c", 0.55),
            ("10.1/d", 0.5),
            ("10.1/e", 0.2),
        ):
            paper = _paper(doi=doi)
            paper.relevance = score
            out.append(paper)
        return out

    def test_none_keeps_everything(self) -> None:
        kept, dropped = relevance.filter_by_score(self._ladder(), top_ratio=None)
        assert len(kept) == 5 and dropped == 0

    def test_half_keeps_the_top_half(self) -> None:
        kept, dropped = relevance.filter_by_score(self._ladder(), top_ratio=0.5)
        assert [p.doi for p in kept] == ["10.1/a", "10.1/b", "10.1/c", "10.1/d"]
        assert dropped == 1

    def test_full_ratio_keeps_only_the_best(self) -> None:
        kept, dropped = relevance.filter_by_score(self._ladder(), top_ratio=1.0)
        assert [p.doi for p in kept] == ["10.1/a"]
        assert dropped == 4

    def test_cutoff_is_relative_to_the_best(self) -> None:
        # Best 0.6, ratio 0.5 -> cutoff 0.3, so 0.2 goes and 0.5 stays.
        ladder = [_paper(doi=d) for d in ("a", "b")]
        ladder[0].relevance = 0.6
        ladder[1].relevance = 0.2
        kept, _ = relevance.filter_by_score(ladder, top_ratio=0.5)
        assert [p.doi for p in kept] == ["a"]

    def test_floors_compose(self) -> None:
        # Ladder 1.0 / 0.6 / 0.55 / 0.5 / 0.2. The absolute floor removes 0.2
        # only; the relative floor then cuts to >= 0.6 of the surviving best
        # (1.0), taking 0.55 and 0.5 with it.
        kept, dropped = relevance.filter_by_score(
            self._ladder(), min_score=0.3, top_ratio=0.6
        )
        assert [p.doi for p in kept] == ["10.1/a", "10.1/b"]
        assert dropped == 3

    def test_unscored_records_are_dropped(self) -> None:
        kept, dropped = relevance.filter_by_score([_paper()], top_ratio=0.5)
        assert kept == [] and dropped == 1

    def test_all_zero_scores_keeps_everything_at_any_ratio(self) -> None:
        # Nothing matched the query; a relative floor has no top to cut below.
        ladder = [_paper(doi="10.1/a"), _paper(doi="10.1/b")]
        for paper in ladder:
            paper.relevance = 0.0
        kept, dropped = relevance.filter_by_score(ladder, top_ratio=0.5)
        assert len(kept) == 2 and dropped == 0


class TestScoreSort:
    def test_ranks_by_stamped_score(self) -> None:
        a, b = _paper(doi="10.1/a"), _paper(doi="10.1/b")
        a.relevance, b.relevance = 0.1, 0.9
        assert [p.doi for p in sort_papers([a, b], "score")] == ["10.1/b", "10.1/a"]

    def test_query_argument_computes_the_score(self) -> None:
        a, b = OFF_TOPIC, ON_TOPIC
        ranked = sort_papers([a, b], "score", query="legal liability")
        assert ranked[0] is b

    def test_unscored_records_tie_last_and_do_not_raise(self) -> None:
        scored, unscored = _paper(doi="10.1/a"), _paper(doi="10.1/b")
        scored.relevance = 0.5
        ranked = sort_papers([scored, unscored], "score")
        assert [p.doi for p in ranked] == ["10.1/a", "10.1/b"]

    def test_all_unscored_does_not_raise(self) -> None:
        # Every mode must work on any list — "score" ties at input order here.
        # Built fresh, not copied: score_papers stamps relevance in place, so
        # earlier tests in this module have already scored the shared constants.
        papers = [
            _paper(doi="10.1/x", title="Global burden of cardiovascular disease"),
            _paper(doi="10.1/y", title="Legal liability for artificial intelligence"),
        ]
        assert all(p.relevance is None for p in papers)
        assert sort_papers(papers, "score") == papers

    def test_ties_keep_input_order(self) -> None:
        a, b = _paper(doi="10.1/a"), _paper(doi="10.1/b")
        a.relevance = b.relevance = 0.4
        assert [p.doi for p in sort_papers([a, b], "score")] == ["10.1/a", "10.1/b"]

    def test_score_is_a_declared_sort_mode(self) -> None:
        assert "score" in rp.SORT_MODES

    def test_relevance_mode_still_preserves_input_order(self) -> None:
        papers = [OFF_TOPIC, ON_TOPIC]
        assert sort_papers(papers, "relevance") == papers


class TestRelevanceRoundTrip:
    def test_score_survives_json(self, tmp_path) -> None:
        papers = relevance.score_papers([ON_TOPIC], "legal liability")
        target = tmp_path / "scored.json"
        rp.to_json(papers, target)
        restored = rp.from_json(target)
        assert restored[0].relevance == papers[0].relevance

    def test_absent_relevance_reads_as_none(self) -> None:
        restored = Paper.from_dict({"title": "x"})
        assert restored.relevance is None

    def test_garbage_relevance_degrades_to_none(self) -> None:
        restored = Paper.from_dict({"title": "x", "relevance": "not-a-number"})
        assert restored.relevance is None


class TestMatrixScoreColumn:
    def test_score_is_available_as_a_column(self) -> None:
        assert "score" in rp.column_names()

    def test_score_column_renders_to_three_places(self) -> None:
        paper = _paper(title="legal liability")
        paper.relevance = 0.5
        assert matrix.matrix_row(paper, ("score",))["score"] == "0.500"

    def test_unscored_record_renders_an_empty_score(self) -> None:
        assert matrix.matrix_row(_paper(), ("score",))["score"] == ""

    def test_score_is_not_a_default_column(self) -> None:
        # The default column set is a documented contract; score is opt-in.
        assert "score" not in matrix.LITERATURE_COLUMNS


class MixedSource(Source):
    """One on-topic and one off-topic hit, for the pipeline-level tests."""

    name = "mixed"
    capabilities = Capabilities(
        search=True, get=False, references=False, citations=False, pdf=False
    )

    def search(self, query, *, limit=None, filters=None):
        on = ON_TOPIC.copy()
        on.doi = "10.1/on"
        on.sources = ["mixed"]
        off = OFF_TOPIC.copy()
        off.doi = "10.1/off"
        off.sources = ["mixed"]
        # Honour filters like a real adapter, so field-scope behaviour (and the
        # zero-result hint it can produce) is exercised rather than bypassed.
        from researcher_python.sources.base import matches_filters

        return [p for p in (on, off) if matches_filters(p, filters, query)]

    def get(self, key):
        # Never used by these tests; `get` stays abstract otherwise.
        raise NotFoundError(self.name, key)


@pytest.fixture()
def mixed_source(monkeypatch):
    """Register :class:`MixedSource` under ``mixed`` for one test."""
    import researcher_python.sources as registry

    monkeypatch.setitem(
        registry._REGISTRY, "mixed", (__name__, MixedSource.__name__)
    )
    return "mixed"


class TestPipelineIntegration:
    """The pipeline stamps scores, applies the floor, and counts what it drops."""

    def test_scores_every_merged_record(self, mixed_source) -> None:
        result = rp.search(
            "legal liability of artificial intelligence",
            sources=[mixed_source],
            settings=rp.Settings(),
        )
        assert result.dropped == 0
        assert all(p.relevance is not None for p in result.papers)

    def test_reports_the_terms_it_scored_against(self, mixed_source) -> None:
        result = rp.search(
            "legal liability", sources=[mixed_source], settings=rp.Settings()
        )
        assert result.terms == ["legal", "liability"]

    def test_min_score_drops_and_counts_the_off_topic_hit(self, mixed_source) -> None:
        result = rp.search(
            "legal liability of artificial intelligence",
            sources=[mixed_source],
            min_score=0.2,
            settings=rp.Settings(),
        )
        assert [p.doi for p in result.papers] == ["10.1/on"]
        assert result.dropped == 1

    def test_sort_score_orders_the_merged_set(self, mixed_source) -> None:
        result = rp.search(
            "legal liability of artificial intelligence",
            sources=[mixed_source],
            sort="score",
            settings=rp.Settings(),
        )
        assert result.papers[0].doi == "10.1/on"

    def test_top_ratio_drops_and_counts_the_weak_hit(self, mixed_source) -> None:
        result = rp.search(
            "legal liability of artificial intelligence",
            sources=[mixed_source],
            top_ratio=0.5,
            settings=rp.Settings(),
        )
        assert [p.doi for p in result.papers] == ["10.1/on"]
        assert result.dropped == 1

    def test_top_ratio_is_validated(self) -> None:
        from researcher_python.errors import ConfigurationError

        with pytest.raises(ConfigurationError, match="top_ratio"):
            rp.search("x", sources=["openalex"], top_ratio=0.0, settings=rp.Settings())
        with pytest.raises(ConfigurationError, match="top_ratio"):
            rp.search("x", sources=["openalex"], top_ratio=2.0, settings=rp.Settings())

    def test_min_score_is_validated(self) -> None:
        from researcher_python.errors import ConfigurationError

        with pytest.raises(ConfigurationError, match="min_score"):
            rp.search("x", sources=["openalex"], min_score=1.5, settings=rp.Settings())

    def test_pipeline_passes_the_field_scope_through(self, mixed_source) -> None:
        from researcher_python.sources.base import SearchFilters

        result = rp.search(
            "legal liability of artificial intelligence",
            sources=[mixed_source],
            filters=SearchFilters(field="title"),
            settings=rp.Settings(),
        )
        # The fake source ignores filters, but the pipeline still scores on the
        # scoped field, so the records are separated by title saturation
        # rather than collapsing to one score.
        assert len({p.relevance for p in result.papers}) == len(result.papers)

class TestFieldScopedScoring:
    """A field-scoped search must still rank, not collapse to one score.

    A ``--field title`` search returns only title-matching records. Scoring
    those on title+abstract+keywords made every one of them saturate at the
    same score, so ``--sort score`` was a no-op and a relative floor could
    never cut anything.
    """

    def _titled(self):
        out = []
        for doi, title in (
            ("10.1/a", "AI Regulation"),
            ("10.1/b", "AI Regulation and the radiologist workforce"),
            ("10.1/c", "Letter to the Editor: AI, regulation, and the radiologist "
                       "workforce reorganization without replacement"),
        ):
            out.append(_paper(doi=doi, title=title))
        return out

    def test_scoring_on_title_only_still_separates_records(self) -> None:
        papers = relevance.score_papers(self._titled(), "AI regulation", ("title",))
        scores = [p.relevance for p in papers]
        assert len(set(scores)) == len(scores), scores
        # The tightest title wins; the looser ones rank below it.
        assert scores[0] > scores[1] > scores[2]

    def test_scoped_scoring_reads_only_the_scoped_field(self) -> None:
        # A record whose *title* is dense with the terms but whose abstract is
        # empty scores on the title alone when scoped to the title.
        paper = _paper(doi="10.1/x", title="AI Regulation")
        terms = relevance.query_terms("AI regulation")
        assert relevance.score_paper(paper, terms, ("title",)) > 0.0
        # Scoped to a field the record does not carry, it scores zero.
        assert relevance.score_paper(paper, terms, ("venue",)) == 0.0

    def test_scoped_scoring_is_not_higher_than_unscoped(self) -> None:
        # Scoping can only remove evidence, never add it.
        paper = _paper(doi="10.1/x", title="AI Regulation and the workforce")
        paper.abstract = "An extended discussion of artificial intelligence " * 12
        terms = relevance.query_terms("AI regulation")
        unscoped = relevance.score_paper(paper, terms)
        scoped = relevance.score_paper(paper, terms, ("title",))
        assert scoped <= unscoped

    def test_unknown_field_falls_back_to_all_fields(self) -> None:
        paper = _paper(doi="10.1/x", title="Liability", abstract="liability law")
        terms = relevance.query_terms("liability")
        assert relevance.score_paper(paper, terms, ("nonexistent",)) == (
            relevance.score_paper(paper, terms)
        )


class TestSearchConvenienceKwargs:
    """field=/year_from=/year_to= mirror the CLI flags and the MCP tool."""

    def test_field_is_a_keyword_on_search(self, mixed_source) -> None:
        # Used to raise TypeError: search() got an unexpected keyword 'field'.
        result = rp.search(
            "legal liability",
            sources=[mixed_source],
            field="title",
            settings=rp.Settings(),
        )
        assert result.papers

    def test_year_scope_is_a_keyword_on_search(self, mixed_source) -> None:
        result = rp.search(
            "legal liability",
            sources=[mixed_source],
            year_from=2000,
            year_to=2030,
            settings=rp.Settings(),
        )
        assert result.papers

    def test_filters_and_kwargs_together_is_a_configuration_error(
        self, mixed_source
    ) -> None:
        from researcher_python.errors import ConfigurationError

        with pytest.raises(ConfigurationError, match="not both"):
            rp.search(
                "x",
                sources=[mixed_source],
                filters=rp.SearchFilters(field="title"),
                field="abstract",
                settings=rp.Settings(),
            )


class TestMinCitations:
    def test_floor_drops_low_cited_and_counts_it(self, mixed_source) -> None:
        result = rp.search(
            "legal liability",
            sources=[mixed_source],
            min_citations=5,
            settings=rp.Settings(),
        )
        # The fake records carry no citation count, and unknown is KEPT.
        assert result.papers
        assert result.dropped == 0

    def test_unknown_citation_count_is_kept_not_hidden(self, mixed_source) -> None:
        result = rp.search(
            "legal liability",
            sources=[mixed_source],
            min_citations=10_000,
            settings=rp.Settings(),
        )
        assert result.papers, "unknown citation counts must never be dropped"

    def test_negative_floor_is_a_configuration_error(self) -> None:
        from researcher_python.errors import ConfigurationError

        with pytest.raises(ConfigurationError, match="min_citations"):
            rp.search("x", sources=["openalex"], min_citations=-1, settings=rp.Settings())


class TestZeroResultHint:
    def test_long_title_query_explains_itself(self, mixed_source) -> None:
        # A field-scoped AND match: every term must appear in the title, so a
        # query with terms the titles lack matches nothing — silently, unless
        # the hint explains why.
        result = rp.search(
            "blockchain quantum surveillance encryption",
            sources=[mixed_source],
            field="title",
            settings=rp.Settings(),
        )
        assert result.papers == []
        assert "requires ALL" in result.hint
        assert "field='any'" in result.hint

    def test_a_found_result_carries_no_hint(self, mixed_source) -> None:
        result = rp.search(
            "legal liability",
            sources=[mixed_source],
            settings=rp.Settings(),
        )
        assert result.papers
        assert result.hint == ""


class TestScoringOverrides:
    """Experimental weights stay opt-in and never change the default score."""

    def test_overrides_equal_to_defaults_reproduce_the_default_score(self) -> None:
        terms = relevance.query_terms("legal liability artificial intelligence")
        assert relevance.score_paper(ON_TOPIC, terms) == relevance.score_paper(
            ON_TOPIC,
            terms,
            field_weights=dict(relevance.FIELD_WEIGHTS),
            phrase_bonus=0.10,
            coverage_weight=0.75,
        )

    def test_coverage_weight_selects_coverage_or_density(self) -> None:
        # A covers every term once; B repeats one term heavily.  Coverage
        # favours A, density favours B, so the weight must decide the winner.
        query = "alpha beta gamma"
        terms = relevance.query_terms(query)
        broad = _paper(title="alpha beta gamma " + "filler " * 30)
        repetitive = _paper(title="alpha " * 40)
        coverage_only = relevance.score_paper(broad, terms, coverage_weight=1.0)
        density_only = relevance.score_paper(broad, terms, coverage_weight=0.0)
        assert coverage_only > density_only
        assert relevance.score_paper(
            repetitive, terms, coverage_weight=0.0
        ) > relevance.score_paper(broad, terms, coverage_weight=0.0)

    def test_field_weight_override_can_promote_the_title(self) -> None:
        terms = relevance.query_terms("liability")
        # The title is denser than the abstract, so weighting it higher raises
        # the density term without either side saturating to 1.0.
        paper = _paper(
            title="On liability law",
            abstract=("liability " * 10) + ("filler " * 30),
        )
        default = relevance.score_paper(paper, terms)
        title_heavy = relevance.score_paper(
            paper, terms, field_weights={"title": 20.0, "abstract": 0.1}
        )
        assert title_heavy > default

    def test_unknown_field_weight_is_ignored(self) -> None:
        terms = relevance.query_terms("liability")
        assert relevance.score_paper(
            ON_TOPIC, terms, field_weights={"not_a_field": 99.0}
        ) == relevance.score_paper(ON_TOPIC, terms)

    def test_negative_field_weight_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            relevance.score_paper(
                ON_TOPIC, relevance.query_terms("liability"), field_weights={"title": -1}
            )

    def test_bad_coverage_weight_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="coverage_weight"):
            relevance.score_paper(
                ON_TOPIC, relevance.query_terms("liability"), coverage_weight=1.5
            )

    def test_negative_phrase_bonus_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="phrase_bonus"):
            relevance.score_paper(
                ON_TOPIC, relevance.query_terms("liability"), phrase_bonus=-0.1
            )
