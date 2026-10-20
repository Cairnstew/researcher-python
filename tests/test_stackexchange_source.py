"""Offline tests for the Stack Exchange adapter.

Pure parsing is pinned against the vendored live-response fixture (D4-style),
and the paging/params behaviour is tested with ``_get`` stubbed — nothing
here touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from researcher_python.config import Settings
from researcher_python.errors import ConfigurationError, RateLimitError, SourceError
from researcher_python.web_sources.stackexchange import (
    SE_SORTS,
    StackExchangeSource,
    parse_stackexchange_question,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "stackexchange_search.json"


def load_fixture() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text())


# ---------------------------------------------------------------------- #
# Pure parsing
# ---------------------------------------------------------------------- #


def test_parse_question_maps_common_fields() -> None:
    item = load_fixture()["items"][0]
    record = parse_stackexchange_question(item)
    assert record.source == "stackoverflow"
    assert record.id == "5946236"
    assert record.title == "How to merge dicts, collecting values from matching keys?"
    assert record.url.startswith("https://stackoverflow.com/questions/5946236")
    assert record.score == 165
    assert record.created == 1305010204
    assert record.author == "Salil"
    assert record.tags == ["python", "dictionary", "merge"]


def test_parse_question_keeps_source_specific_fields_in_extra() -> None:
    item = load_fixture()["items"][0]
    record = parse_stackexchange_question(item)
    assert record.extra["answer_count"] == 13
    assert record.extra["view_count"] == 313650
    assert record.extra["is_answered"] is True
    # Captured fields are not duplicated into extra.
    assert "title" not in record.extra
    assert "owner" not in record.extra
    assert "tags" not in record.extra
    assert "score" not in record.extra


def test_parse_question_source_uses_the_site_name() -> None:
    item = load_fixture()["items"][0]
    record = parse_stackexchange_question(item, source="superuser")
    assert record.source == "superuser"


def test_parse_question_builds_a_url_from_the_id_when_link_is_missing() -> None:
    record = parse_stackexchange_question({"question_id": 42, "title": "q"})
    assert record.url == "https://stackoverflow.com/questions/42"
    record = parse_stackexchange_question(
        {"question_id": 42, "title": "q"}, source="serverfault"
    )
    assert record.url == "https://serverfault.com/questions/42"


def test_parse_question_tolerates_missing_and_wrong_shaped_fields() -> None:
    record = parse_stackexchange_question({})
    assert record.id == ""
    assert record.title == ""
    assert record.score is None
    assert record.created is None
    assert record.author == ""
    assert record.tags == []
    assert record.extra == {}
    assert record.url == ""


def test_parse_question_ignores_non_mapping_items() -> None:
    record = parse_stackexchange_question(None)  # type: ignore[arg-type]
    assert record.id == ""
    assert record.extra == {}


def test_parse_question_unescapes_html_entities() -> None:
    record = parse_stackexchange_question(
        {
            "question_id": 1,
            "title": "How to &#39;deep-merge&#39; dicts?",
            "link": "https://stackoverflow.com/questions/1",
            "owner": {"display_name": "Sean &amp; Co"},
        }
    )
    assert record.title == "How to 'deep-merge' dicts?"
    assert record.author == "Sean & Co"


# ---------------------------------------------------------------------- #
# Source: params + search paging (stubbed _get)
# ---------------------------------------------------------------------- #


class _Capture:
    """Records every ``_get`` call; returns scripted payloads."""

    def __init__(self, *pages: tuple[Any, bool]) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def respond(self, endpoint: str, params: dict[str, Any]) -> Any:
        self.calls.append((endpoint, params))
        payload, raise_rate = self.pages.pop(0)
        if raise_rate:
            raise RateLimitError("stackoverflow", "throttle_violation")
        return payload


def source_with(monkeypatch: pytest.MonkeyPatch, capture: _Capture) -> StackExchangeSource:
    source = StackExchangeSource(Settings())
    source._throttle.min_interval = 0.0  # never wait in tests
    source._get = capture.respond  # type: ignore[method-assign]
    return source


def test_query_params_include_site_filter_and_key() -> None:
    source = StackExchangeSource(Settings(stackexchange_key="appkey"))
    params = source.query_params("merge dicts", "relevance", 2, 50)
    assert params["site"] == "stackoverflow"
    assert params["intitle"] == "merge dicts"
    assert params["sort"] == "relevance"
    assert params["order"] == "desc"
    assert params["page"] == 2
    assert params["pagesize"] == 50
    assert params["filter"] == "default"
    assert params["key"] == "appkey"


def test_query_params_omit_key_when_unconfigured() -> None:
    source = StackExchangeSource(Settings())
    assert "key" not in source.query_params("q", "votes", 1, 10)


def test_search_parses_one_page_and_stops_when_done(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = load_fixture()
    payload = {**payload, "has_more": False}
    capture = _Capture((payload, False))
    source = source_with(monkeypatch, capture)

    result = source.search("merge dicts", max_results=10)
    assert len(result.results) == 2
    assert result.results[0].id == "5946236"
    assert result.pages == 1
    assert result.truncated is False
    assert result.quota == {"max": 300, "remaining": 299}

    endpoint, params = capture.calls[0]
    assert endpoint == "/search"
    assert params["pagesize"] == 10  # min(page_size, want)
    assert params["intitle"] == "merge dicts"


def test_search_pages_until_has_more_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    page1 = load_fixture()  # has_more: true, 2 items
    page2 = {**load_fixture(), "has_more": False}
    capture = _Capture((page1, False), (page2, False))
    source = source_with(monkeypatch, capture)

    result = source.search("merge dicts", max_results=10)
    assert len(result.results) == 4
    assert result.pages == 2
    assert result.truncated is False
    assert [calls[1]["page"] for calls in capture.calls] == [1, 2]
    # Second page asked for the remaining 8, but the API answered 2.
    assert capture.calls[1][1]["pagesize"] == 8


def test_search_truncates_at_max_results(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _Capture((load_fixture(), False))
    source = source_with(monkeypatch, capture)

    result = source.search("merge dicts", max_results=1)
    assert len(result.results) == 1
    assert result.pages == 1
    assert result.truncated is True  # has_more said there was more


def test_search_empty_query_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _Capture()
    source = source_with(monkeypatch, capture)
    result = source.search("   ")
    assert result.results == []
    assert result.pages == 0
    assert capture.calls == []


def test_search_zero_max_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _Capture()
    source = source_with(monkeypatch, capture)
    result = source.search("merge dicts", max_results=0)
    assert result.results == []
    assert capture.calls == []


def test_search_max_defaults_to_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    page = {**load_fixture(), "has_more": False}
    capture = _Capture((page, False))
    source = StackExchangeSource(Settings(max_results=4))
    source._throttle.min_interval = 0.0
    source._get = capture.respond  # type: ignore[method-assign]

    result = source.search("merge dicts")
    assert capture.calls[0][1]["pagesize"] == 4


def test_search_validates_sort(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _Capture()
    source = source_with(monkeypatch, capture)
    with pytest.raises(ConfigurationError) as excinfo:
        source.search("merge dicts", sort="popularity")
    assert "popularity" in str(excinfo.value)
    assert "relevance" in str(excinfo.value)
    assert capture.calls == []


def test_search_sorts_map_to_the_api_sorts() -> None:
    assert SE_SORTS == ("relevance", "activity", "votes", "creation")
    for sort in SE_SORTS:
        params = StackExchangeSource().query_params("q", sort, 1, 10)
        assert params["sort"] == sort


def test_search_propagates_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(endpoint: str, params: dict[str, Any]) -> Any:  # noqa: ANN001
        raise SourceError("stackoverflow", "request failed")

    source = StackExchangeSource(Settings())
    source._throttle.min_interval = 0.0
    source._get = boom  # type: ignore[method-assign]
    with pytest.raises(SourceError):
        source.search("merge dicts")


def test_site_kwarg_is_reflected_in_params(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _Capture(({**load_fixture(), "has_more": False}, False))
    source = StackExchangeSource(Settings(), site="serverfault")
    source._throttle.min_interval = 0.0
    source._get = capture.respond  # type: ignore[method-assign]

    source.search("vim", max_results=2)
    assert capture.calls[0][1]["site"] == "serverfault"