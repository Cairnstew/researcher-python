"""The two new sources: ACM (a prefix-scoped Crossref) and OpenAIRE (grey lit)."""

from __future__ import annotations

import json

import pytest

from researcher_python.errors import ConfigurationError, NotFoundError
from researcher_python.models import Paper
from researcher_python.sources import create_source, get_source_class, source_names
from researcher_python.sources.base import SearchFilters

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------- #
# Registry
# ---------------------------------------------------------------------- #


class TestRegistry:
    def test_both_are_registered(self) -> None:
        assert "acm" in source_names()
        assert "openaire" in source_names()

    def test_neither_is_a_default_source(self) -> None:
        # Both are subsets of Crossref's reach; adding them to every
        # unqualified search would only inflate the merged result set.
        from researcher_python.sources import DEFAULT_SOURCES

        assert "acm" not in DEFAULT_SOURCES
        assert "openaire" not in DEFAULT_SOURCES

    @pytest.mark.parametrize(
        ("name", "expected"),
        [("acm", "ACMSource"), ("openaire", "OpenAIRESource")],
    )
    def test_registry_resolves_to_the_class(self, name: str, expected: str) -> None:
        assert get_source_class(name).__name__ == expected

    def test_both_declare_only_what_they_can_do(self) -> None:
        assert create_source("acm").capabilities.citations is False
        assert create_source("openaire").capabilities.pdf is False

    def test_both_build(self) -> None:
        assert create_source("acm").name == "acm"
        assert create_source("openaire").name == "openaire"


# ---------------------------------------------------------------------- #
# ACM
# ---------------------------------------------------------------------- #


def _item(doi: str, title: str, year: str | None = "2020") -> dict:
    return {
        "DOI": doi,
        "title": [title],
        "issued": {"date-parts": [[int(year)]]} if year else [],
        "author": [{"family": "Doe", "given": "Jane"}],
        "type": "proceedings-article",
    }


@pytest.fixture
def acm(monkeypatch):
    """An ACMSource with its transport replaced and the rate gate disabled."""
    from researcher_python.sources.acm_source import ACMSource

    calls: list[dict] = []

    def fake_payload(self, **kwargs):
        calls.append(kwargs)
        return {"items": list(self._items)}

    source = ACMSource()
    source._items = [_item("10.1145/1234.5678", "An ACM paper")]
    monkeypatch.setattr(ACMSource, "_payload", fake_payload)
    monkeypatch.setattr(ACMSource, "_gate", type("G", (), {"wait": lambda s: 0})(), raising=False)
    source.calls = calls  # type: ignore[attr-defined]
    return source


class TestACMSearch:
    def test_scopes_every_query_to_the_acm_prefix(self, acm) -> None:
        acm.search("agents", limit=3)
        assert acm.calls[0]["filter"] == {"prefix": "10.1145"}

    def test_returns_the_parsed_record(self, acm) -> None:
        papers = acm.search("agents", limit=3)
        assert [p.doi for p in papers] == ["10.1145/1234.5678"]
        assert papers[0].sources == ["acm"]

    def test_drops_a_non_acm_record_that_slips_through(self, acm) -> None:
        acm._items = [
            _item("10.1145/1.1", "Real ACM paper"),
            _item("10.9999/2.2", "A non-ACM paper"),
        ]
        assert [p.doi for p in acm.search("agents", limit=5)] == ["10.1145/1.1"]

    def test_a_title_scoped_query_uses_the_title_key(self, acm) -> None:
        acm.search("agents", limit=3, filters=SearchFilters(field="title"))
        assert acm.calls[0]["query_title"] == "agents"
        assert "query" not in acm.calls[0]

    def test_the_year_window_is_translated(self, acm) -> None:
        acm.search("agents", limit=3, filters=SearchFilters(year_from=2019, year_to=2021))
        assert acm.calls[0]["filter"] == {
            "prefix": "10.1145",
            "from-pub-date": "2019-01-01",
            "until-pub-date": "2021-12-31",
        }

    def test_an_empty_query_makes_no_request(self, acm) -> None:
        assert acm.search("   ", limit=3) == []
        assert acm.calls == []

    def test_a_zero_limit_makes_no_request(self, acm) -> None:
        assert acm.search("agents", limit=0) == []
        assert acm.calls == []


class TestACMGet:
    def test_refuses_a_doi_outside_the_acm_prefix(self, acm) -> None:
        with pytest.raises(NotFoundError, match="10.1145"):
            acm.get("10.9999/nope")

    def test_resolves_an_acm_doi(self, acm) -> None:
        # `get` resolves through `_payload(ids=...)`, so override that on the
        # instance rather than the class the search fixture already patched.
        acm._payload = lambda **kwargs: _item("10.1145/1234.5678", "An ACM paper")
        paper = acm.get("10.1145/1234.5678")
        assert paper.doi == "10.1145/1234.5678"


def _entity(**overrides) -> dict:
    """One OpenAIRE ``oaf:entity`` wrapper around a result record."""
    record = {
        "title": [{"$": "Ethical and Social Issues in the Information Age"}],
        "description": {"$": "A seventh edition survey."},
        "creator": [{"$": "Kizza, Joseph Migga"}],
        "dateofacceptance": {"$": "2023-02-01"},
        "publisher": {"$": "Springer"},
        "subject": [{"$": "computer ethics"}, {"$": "information society"}],
        "resulttype": {"$": "other"},
        "bestaccessright": {"$": "OPEN"},
        "pid": [{"@classid": "doi", "$": "10.1007/978-3-031-24863-4"}],
        "url": {"$": "https://link.springer.com/book/10.1007/978-3-031-24863-4"},
    }
    record.update(overrides)
    return {"oaf:entity": {"oaf:result": record}}


class TestOpenAIREParser:
    def test_title_abstract_and_year(self) -> None:
        from researcher_python.sources.openaire_source import parse_openaire_result

        paper = parse_openaire_result(_entity())
        assert paper.title == "Ethical and Social Issues in the Information Age"
        assert paper.abstract == "A seventh edition survey."
        assert paper.year == 2023

    def test_authors_and_subjects(self) -> None:
        from researcher_python.sources.openaire_source import parse_openaire_result

        paper = parse_openaire_result(_entity())
        assert paper.authors[0].name == "Kizza, Joseph Migga"
        assert paper.keywords == ["computer ethics", "information society"]

    def test_doi_is_extracted_from_the_pid_list(self) -> None:
        from researcher_python.sources.openaire_source import parse_openaire_result

        paper = parse_openaire_result(_entity())
        assert paper.doi == "10.1007/978-3-031-24863-4"
        assert paper.ids["doi"] == "10.1007/978-3-031-24863-4"

    def test_the_other_result_type_reads_as_a_report(self) -> None:
        from researcher_python.sources.openaire_source import parse_openaire_result

        assert parse_openaire_result(_entity()).work_type == "report"

    def test_dataset_result_type(self) -> None:
        from researcher_python.sources.openaire_source import parse_openaire_result

        assert parse_openaire_result(_entity(resulttype={"$": "dataset"})).work_type == "dataset"

    def test_open_access_is_read_from_bestaccessright(self) -> None:
        from researcher_python.sources.openaire_source import parse_openaire_result

        paper = parse_openaire_result(_entity())
        assert paper.is_open_access is True
        closed = parse_openaire_result(_entity(bestaccessright={"$": "CLOSED"}))
        assert closed.is_open_access is False

    def test_a_second_title_becomes_a_subtitle(self) -> None:
        from researcher_python.sources.openaire_source import parse_openaire_result

        record = _entity(title=[{"$": "Main title"}, {"$": "The subtitle"}])
        assert parse_openaire_result(record).title == "Main title: The subtitle"

    def test_a_non_dict_input_yields_an_empty_paper(self) -> None:
        from researcher_python.sources.openaire_source import parse_openaire_result

        assert parse_openaire_result(None).title == ""

    def test_a_missing_date_leaves_the_year_unknown(self) -> None:
        from researcher_python.sources.openaire_source import parse_openaire_result

        paper = parse_openaire_result(_entity(dateofacceptance=None))
        assert paper.year is None


@pytest.fixture
def openaire(monkeypatch):
    """An OpenAIRESource with its transport replaced and the gate disabled."""
    from researcher_python.sources.openaire_source import OpenAIRESource

    calls: list[dict] = []

    def fake_get(self, params):
        calls.append(params)
        return self._payloads.pop(0) if self._payloads else {"response": {}}

    source = OpenAIRESource()
    source._payloads = [  # type: ignore[attr-defined]
        {"response": {"results": {"result": [_entity()]}}}
    ]
    monkeypatch.setattr(OpenAIRESource, "_get", fake_get)
    monkeypatch.setattr(OpenAIRESource, "_gate", type("G", (), {"wait": lambda s: 0})(), raising=False)
    source.calls = calls  # type: ignore[attr-defined]
    return source


class TestOpenAIRESearch:
    def test_returns_parsed_records(self, openaire) -> None:
        papers = openaire.search("information age", limit=5)
        assert papers[0].doi == "10.1007/978-3-031-24863-4"

    def test_keywords_are_the_default_query_key(self, openaire) -> None:
        openaire.search("information age", limit=5)
        assert openaire.calls[0]["keywords"] == "information age"

    def test_a_title_scope_uses_the_title_key(self, openaire) -> None:
        openaire.search("information age", limit=5, filters=SearchFilters(field="title"))
        assert openaire.calls[0]["title"] == "information age"
        assert "keywords" not in openaire.calls[0]

    def test_the_year_window_uses_accepted_dates(self, openaire) -> None:
        openaire.search("x", limit=5, filters=SearchFilters(year_from=2019, year_to=2021))
        assert openaire.calls[0]["fromDateAccepted"] == "2019-01-01"
        assert openaire.calls[0]["toDateAccepted"] == "2021-12-31"

    def test_a_result_type_is_forwarded(self, openaire) -> None:
        openaire.search("x", limit=5, result_type="other")
        assert openaire.calls[0]["resulttype"] == "other"

    def test_an_unknown_result_type_is_rejected(self, openaire) -> None:
        with pytest.raises(ConfigurationError, match="unknown result type"):
            openaire.search("x", limit=5, result_type="thesis")

    def test_an_empty_query_makes_no_request(self, openaire) -> None:
        assert openaire.search("  ", limit=5) == []
        assert openaire.calls == []


class TestOpenAIREGet:
    def test_refuses_a_non_doi(self, openaire) -> None:
        with pytest.raises(NotFoundError, match="DOIs only"):
            openaire.get("1706.03762")

    def test_resolves_a_doi(self, openaire) -> None:
        assert openaire.get("10.1007/978-3-031-24863-4").year == 2023

    def test_an_empty_result_is_not_found(self, openaire) -> None:
        openaire._payloads = [{"response": {"results": {"result": []}}}]  # type: ignore[attr-defined]
        with pytest.raises(NotFoundError):
            openaire.get("10.1007/978-3-031-24863-4")


class TestResultExtraction:
    def test_tolerates_an_empty_envelope(self) -> None:
        from researcher_python.sources.openaire_source import OpenAIRESource

        assert OpenAIRESource._results({}) == []
        assert OpenAIRESource._results({"response": {}}) == []

    def test_reads_a_list_result(self) -> None:
        from researcher_python.sources.openaire_source import OpenAIRESource

        payload = {"response": {"results": {"result": [_entity(), _entity()]}}}
        assert len(OpenAIRESource._results(payload)) == 2

    def test_reads_a_single_result(self) -> None:
        from researcher_python.sources.openaire_source import OpenAIRESource

        payload = {"response": {"results": {"result": _entity()}}}
        assert len(OpenAIRESource._results(payload)) == 1