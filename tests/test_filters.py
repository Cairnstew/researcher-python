"""SearchFilters: year window, field scope, and per-adapter translation."""

from __future__ import annotations

import pytest

from researcher_python.models import Paper
from researcher_python.pipeline import _candidates
from researcher_python.sources.base import (
    SEARCH_FIELDS,
    SearchFilters,
    apply_filters,
    matches_filters,
)


def _paper(title: str = "AI agents in society", abstract: str = "", year: int | None = 2020) -> Paper:
    return Paper(title=title, abstract=abstract, year=year)


def _settings_with_email():
    """PubMed refuses to construct without an email (NCBI's polite pool)."""
    from researcher_python.config import Settings

    return Settings(email="tester@example.com")


class TestValidation:
    def test_defaults_are_inactive(self) -> None:
        filters = SearchFilters()
        assert filters.field == "any"
        assert filters.year_from is None and filters.year_to is None
        assert filters.active is False

    def test_any_year_or_field_makes_it_active(self) -> None:
        assert SearchFilters(year_from=2019).active is True
        assert SearchFilters(field="title").active is True

    @pytest.mark.parametrize("field", SEARCH_FIELDS)
    def test_every_documented_field_is_accepted(self, field: str) -> None:
        assert SearchFilters(field=field).field == field

    def test_unknown_field_is_rejected_with_the_legal_values(self) -> None:
        with pytest.raises(ValueError, match="unknown search field"):
            SearchFilters(field="keyword")

    @pytest.mark.parametrize("bad", [99, 3000, 0])
    def test_implausible_years_are_rejected(self, bad: int) -> None:
        with pytest.raises(ValueError, match="plausible year"):
            SearchFilters(year_from=bad)

    def test_non_integer_year_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be an int"):
            SearchFilters(year_to="soon")

    def test_a_bool_is_not_accepted_as_a_year(self) -> None:
        with pytest.raises(ValueError, match="must be an int"):
            SearchFilters(year_from=True)


class TestYearWindow:
    def test_bounds_are_inclusive(self) -> None:
        filters = SearchFilters(year_from=2019, year_to=2021)
        assert filters.year_ok(2019) is True
        assert filters.year_ok(2021) is True
        assert filters.year_ok(2018) is False
        assert filters.year_ok(2022) is False

    def test_a_record_with_no_year_is_kept(self) -> None:
        # Dropping undated records would hide exactly the grey literature a
        # reader most needs to see and judge.
        assert SearchFilters(year_from=2019).year_ok(None) is True

    def test_only_a_lower_bound(self) -> None:
        assert SearchFilters(year_from=2020).year_ok(1999) is False

    def test_only_an_upper_bound(self) -> None:
        assert SearchFilters(year_to=2000).year_ok(2001) is False


class TestFieldScope:
    def test_title_scope_requires_every_term_in_the_title(self) -> None:
        filters = SearchFilters(field="title")
        assert matches_filters(_paper("AI agents in society"), filters, "ai agents") is True
        assert matches_filters(_paper("AI agents in society"), filters, "ai robots") is False

    def test_abstract_scope_reads_the_abstract(self) -> None:
        filters = SearchFilters(field="abstract")
        paper = _paper(title="Unrelated title", abstract="Agents matter here")
        assert matches_filters(paper, filters, "agents") is True
        assert matches_filters(paper, filters, "unrelated") is False

    def test_boolean_operators_are_not_terms(self) -> None:
        filters = SearchFilters(field="title")
        assert matches_filters(_paper("AI agents in society"), filters, "agents AND society") is True

    def test_an_api_style_field_prefix_is_stripped(self) -> None:
        filters = SearchFilters(field="title")
        assert matches_filters(_paper("AI agents"), filters, "ti:(agents)") is True

    def test_field_any_accepts_anything(self) -> None:
        assert matches_filters(_paper("x"), SearchFilters(), "anything at all") is True


class TestApplyFilters:
    def test_preserves_order_and_drops_the_rest(self) -> None:
        papers = [_paper("a", year=2018), _paper("b", year=2020), _paper("c", year=2021)]
        kept = apply_filters(papers, SearchFilters(year_from=2019))
        assert [p.title for p in kept] == ["b", "c"]

    def test_inactive_filters_return_the_same_list_object(self) -> None:
        papers = [_paper("a")]
        assert apply_filters(papers, None) is papers
        assert apply_filters(papers, SearchFilters()) is papers

    def test_filters_combine(self) -> None:
        papers = [
            _paper("ai agents", year=2020),
            _paper("ai agents", year=2010),
            _paper("unrelated", year=2020),
        ]
        kept = apply_filters(papers, SearchFilters(year_from=2019, field="title"), "ai agents")
        assert len(kept) == 1


class TestGetRouting:
    def test_isbn_routes_to_the_book_capable_sources(self) -> None:
        assert _candidates("978-3-031-24863-4", None) == ["openalex", "crossref"]

    def test_doi_routing_is_unchanged(self) -> None:
        assert _candidates("10.1/x", None) == ["openalex", "crossref", "semantic_scholar"]


class TestAdapterTranslation:
    """Each adapter must state the window/scope in its own API's dialect."""

    def test_openalex_uses_filter_keys(self, monkeypatch) -> None:
        from researcher_python.sources.openalex_source import OpenAlexSource

        calls: list[dict] = []

        def fake_works(self, params):
            calls.append(params)
            return []

        monkeypatch.setattr(OpenAlexSource, "_works", fake_works)
        monkeypatch.setattr(OpenAlexSource, "_gate", type("G", (), {"wait": lambda self: 0})(), raising=False)
        source = OpenAlexSource()
        source.search(
            "agents",
            limit=5,
            filters=SearchFilters(year_from=2019, field="title"),
        )
        expression = calls[0]["filter"]
        assert "title.search:agents" in expression
        assert "from_publication_date:2019-01-01" in expression

    def test_crossref_uses_its_own_filter_and_query_keys(self, monkeypatch) -> None:
        from researcher_python.sources.crossref_source import CrossrefSource

        calls: list[dict] = []

        def fake_payload(self, **kwargs):
            calls.append(kwargs)
            return {"items": []}

        monkeypatch.setattr(CrossrefSource, "_payload", fake_payload)
        monkeypatch.setattr(CrossrefSource, "_gate", type("G", (), {"wait": lambda self: 0})(), raising=False)
        source = CrossrefSource()
        source.search(
            "agents", limit=5, filters=SearchFilters(year_from=2019, field="title")
        )
        assert calls[0]["query_title"] == "agents"
        assert calls[0]["filter"] == {"from-pub-date": "2019-01-01"}
        assert "query" not in calls[0]

    def test_pubmed_builds_a_field_tag_and_a_date_filter(self, monkeypatch) -> None:
        from researcher_python.sources.pubmed_source import PubMedSource

        seen: list[str] = []

        def fake_esearch(self, term, retmax):
            seen.append(term)
            return []

        monkeypatch.setattr(PubMedSource, "_esearch", fake_esearch)
        monkeypatch.setattr(PubMedSource, "_gate", type("G", (), {"wait": lambda self: 0})(), raising=False)
        PubMedSource(settings=_settings_with_email()).search(
            "agents", limit=5, filters=SearchFilters(year_from=2019, field="title")
        )
        # The multi-term query is parenthesised so the AND binds the whole term.
        assert seen[0] == "(agents[ti]) AND 2019:3000[dp]"

    def test_pubmed_without_a_window_adds_no_date_clause(self, monkeypatch) -> None:
        from researcher_python.sources.pubmed_source import PubMedSource

        seen: list[str] = []

        monkeypatch.setattr(
            PubMedSource, "_esearch", lambda self, term, retmax: seen.append(term) or []
        )
        monkeypatch.setattr(PubMedSource, "_gate", type("G", (), {"wait": lambda self: 0})(), raising=False)
        PubMedSource(settings=_settings_with_email()).search(
            "agents", limit=5, filters=SearchFilters(field="abstract")
        )
        assert seen[0] == "agents[ab]"

    def test_arxiv_prefixes_the_query_with_its_own_field_tag(self, monkeypatch) -> None:
        from researcher_python.sources.arxiv_source import ArxivSource

        seen: list[dict] = []

        monkeypatch.setattr(
            ArxivSource,
            "_search",
            lambda self, **kwargs: seen.append(kwargs) or [],
        )
        ArxivSource().search("agents", limit=5, filters=SearchFilters(field="title"))
        assert seen[0]["query"] == "ti:(agents)"

    def test_semantic_scholar_filters_locally_and_dedupes_across_pages(
        self, monkeypatch
    ) -> None:
        from researcher_python.sources.semantic_scholar_source import SemanticScholarSource

        pages = [
            {"data": [
                {"paperId": "1", "title": "Agents in society", "year": 2020},
                {"paperId": "2", "title": "Something else", "year": 2020},
            ]},
            {"data": [{"paperId": "1", "title": "Agents in society", "year": 2020}]},
        ]
        calls = {"n": 0}

        def fake_get(self, path, params):
            payload = pages[min(calls["n"], len(pages) - 1)]
            calls["n"] += 1
            return payload

        monkeypatch.setattr(SemanticScholarSource, "_get", fake_get)
        monkeypatch.setattr(SemanticScholarSource, "_gate", type("G", (), {"wait": lambda self: 0})(), raising=False)
        found = SemanticScholarSource().search(
            "agents", limit=5, filters=SearchFilters(field="title")
        )
        # The repeat of paper 1 on page 2 must not be counted twice.
        assert [p.title for p in found] == ["Agents in society"]