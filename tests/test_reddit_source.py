"""Offline tests for the Reddit adapter.

Pure parsing is pinned against ``reddit_search.json``, which is *constructed
from the documented Listing shape* rather than captured live — Reddit's
anonymous JSON endpoints answer HTTP 403 to keyless callers, so a live
fixture needs OAuth app credentials.  Paging, params, auth and the header
rate hint are tested with ``_get`` stubbed / the token endpoint stubbed —
nothing here touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from researcher_python.config import Settings
from researcher_python.errors import ConfigurationError, RateLimitError, SourceError
from researcher_python.web_sources import base as web_base
from researcher_python.web_sources import reddit as reddit_mod
from researcher_python.web_sources.reddit import (
    REDDIT_SORTS,
    REDDIT_TIME_RANGES,
    RedditSource,
    parse_reddit_link,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "reddit_search.json"


def load_fixture(**mutations: Any) -> dict[str, Any]:
    payload = json.loads(_FIXTURE.read_text())
    data = dict(payload["data"])
    data.update(mutations)
    return {"kind": "Listing", "data": data}


def link_data(index: int = 0) -> dict[str, Any]:
    return dict(load_fixture()["data"]["children"][index]["data"])


def source_with(monkeypatch: pytest.MonkeyPatch, capture: Any) -> RedditSource:
    """A RedditSource with the token call and network both stubbed."""
    source = RedditSource(Settings(reddit_client_id="cid", reddit_client_secret="csec"))
    source._throttle.min_interval = 0.0  # never wait in tests
    source._ensure_token = lambda: None  # type: ignore[method-assign]
    source._get = capture.respond  # type: ignore[method-assign]
    return source


class _Capture:
    """Records every ``_get`` call; returns scripted payloads."""

    def __init__(self, *pages: tuple[Any, bool]) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def respond(self, endpoint: str, params: dict[str, Any]) -> Any:
        self.calls.append((endpoint, params))
        payload, raise_rate = self.pages.pop(0)
        if raise_rate:
            raise RateLimitError("reddit", "throttle_violation")
        return payload


# ---------------------------------------------------------------------- #
# Pure parsing
# ---------------------------------------------------------------------- #


def test_parse_link_maps_common_fields() -> None:
    record = parse_reddit_link(link_data(0))
    assert record.source == "reddit"
    assert record.id == "abc123"
    assert record.title == "How do I merge two dictionaries in Python?"
    assert record.url.startswith("https://www.reddit.com/r/learnpython/comments/abc123")
    assert record.score == 847
    assert record.created == 1689876543
    assert record.author == "some_pythonista"


def test_parse_link_tags_carry_subreddit_and_flair() -> None:
    record = parse_reddit_link(link_data(0))
    assert record.tags == ["learnpython", "Resource"]


def test_parse_link_tags_skip_missing_flair() -> None:
    record = parse_reddit_link(link_data(1))
    assert record.tags == ["Python"]


def test_parse_link_keeps_source_specific_fields_in_extra() -> None:
    record = parse_reddit_link(link_data(0))
    assert record.extra["num_comments"] == 63
    assert record.extra["upvote_ratio"] == 0.97
    assert record.extra["is_self"] is True
    # Captured fields are not duplicated into extra.
    assert "title" not in record.extra
    assert "subreddit" not in record.extra
    assert "selftext" not in record.extra
    assert "score" not in record.extra


def test_parse_link_uses_the_thread_permalink_as_url() -> None:
    # Even for an external-link post the citable URL is the reddit thread.
    record = parse_reddit_link(link_data(1))
    assert record.url.startswith("https://www.reddit.com/r/Python/comments/def456")


def test_parse_link_builds_url_from_link_target_without_permalink() -> None:
    item = {k: v for k, v in link_data(1).items() if k != "permalink"}
    record = parse_reddit_link(item)
    assert record.url == "https://docs.python.org/3/whatsnew/3.9.html"


def test_parse_link_tolerates_missing_and_wrong_shaped_fields() -> None:
    record = parse_reddit_link({})
    assert record.id == ""
    assert record.title == ""
    assert record.url == ""
    assert record.score is None
    assert record.created is None
    assert record.author == ""
    assert record.tags == []
    assert record.extra == {}


def test_parse_link_ignores_non_mapping_items() -> None:
    record = parse_reddit_link(None)  # type: ignore[arg-type]
    assert record.id == ""
    assert record.extra == {}


# ---------------------------------------------------------------------- #
# Source: params + search paging (stubbed _get)
# ---------------------------------------------------------------------- #


def test_query_params_basics() -> None:
    source = RedditSource(Settings())
    params = source.query_params("merge dicts", "relevance", 1, 25)
    assert params["q"] == "merge dicts"
    assert params["limit"] == 25
    assert params["sort"] == "relevance"
    assert params["raw_json"] == 1
    assert "after" not in params
    assert "count" not in params
    assert "t" not in params  # all == the API default, not sent


def test_query_params_page_two_carries_cursor_and_count(monkeypatch: pytest.MonkeyPatch) -> None:
    source = source_with(monkeypatch, _Capture())
    source._next_after = "t3_xyz789"
    params = source.query_params("q", "top", 2, 10)
    assert params["after"] == "t3_xyz789"
    assert params["count"] == 10


def test_query_params_page_one_resets_the_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    source = source_with(monkeypatch, _Capture())
    source._next_after = "t3_stale"  # left over from a previous search
    params = source.query_params("q", "relevance", 1, 5)
    assert "after" not in params
    assert source._next_after is None


def test_query_params_subreddit_and_time_range_are_enforced() -> None:
    source = RedditSource(Settings(), subreddit="r/python", time_range="month")
    params = source.query_params("q", "relevance", 1, 10)
    assert params["restrict_sr"] == 1
    assert params["sr"] == "python"  # r/ prefix stripped
    assert params["t"] == "month"


def test_query_params_default_time_all_is_omitted() -> None:
    source = RedditSource(Settings())
    assert "t" not in source.query_params("q", "relevance", 1, 10)
    assert len(REDDIT_TIME_RANGES) == 6
    assert REDDIT_TIME_RANGES[0] == "hour"
    assert REDDIT_TIME_RANGES[-1] == "all"


def test_search_parses_one_page_and_stops_when_done(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _Capture((load_fixture(after=None), False))
    source = source_with(monkeypatch, capture)

    result = source.search("merge dicts", max_results=10)
    assert len(result.results) == 2
    assert result.results[0].id == "abc123"
    assert result.pages == 1
    assert result.truncated is False
    assert result.quota == {"max": None, "remaining": None}  # header-based, no body quota

    endpoint, params = capture.calls[0]
    assert endpoint == "/search"
    assert params["limit"] == 10  # min(page_size, want)
    assert params["q"] == "merge dicts"


def test_search_pages_along_the_after_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    page1 = load_fixture(after="t3_XYZ", dist=2)
    # Second page: one record, and no cursor left behind.
    page2 = load_fixture(after=None, dist=1, children=load_fixture()["data"]["children"][:1])
    capture = _Capture((page1, False), (page2, False))
    source = source_with(monkeypatch, capture)

    result = source.search("merge dicts", max_results=10)
    assert len(result.results) == 3
    assert result.pages == 2
    assert result.truncated is False
    # Second request carried the first response's cursor and the right count.
    params2 = capture.calls[1][1]
    assert params2["after"] == "t3_XYZ"
    # count = (page - 1) * pagesize, where pagesize shrank to the remaining
    # want (10 - 2 = 8) — the base loop asks only for what is left.
    assert params2["count"] == 8


def test_search_truncates_when_after_still_points_at_more(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _Capture((load_fixture(), False))  # after is set (more behind it)
    source = source_with(monkeypatch, capture)

    result = source.search("merge dicts", max_results=1)
    assert len(result.results) == 1
    assert result.pages == 1
    assert result.truncated is True  # the cursor said there was more


def test_search_empty_query_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _Capture()
    source = source_with(monkeypatch, capture)
    result = source.search("   ")
    assert result.results == []
    assert result.pages == 0
    assert capture.calls == []


def test_search_validates_sort(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _Capture()
    source = source_with(monkeypatch, capture)
    with pytest.raises(ConfigurationError) as excinfo:
        source.search("merge dicts", sort="popularity")
    assert "popularity" in str(excinfo.value)
    assert "relevance" in str(excinfo.value)
    assert capture.calls == []


def test_search_sorts_match_the_api_sorts() -> None:
    assert REDDIT_SORTS == ("relevance", "hot", "top", "new", "comments")
    for sort in REDDIT_SORTS:
        params = RedditSource(Settings()).query_params("q", sort, 1, 10)
        assert params["sort"] == sort


# ---------------------------------------------------------------------- #
# Auth
# ---------------------------------------------------------------------- #


def test_no_credentials_is_a_configuration_error() -> None:
    # Real _get: _ensure_token must raise before anything touches the network.
    source = RedditSource(Settings())  # no client id/secret
    source._throttle.min_interval = 0.0
    with pytest.raises(ConfigurationError) as excinfo:
        source.search("merge dicts")
    assert "RESEARCHER_REDDIT_CLIENT_ID" in str(excinfo.value)


def test_token_fetch_uses_basic_auth_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    source = RedditSource(Settings(reddit_client_id="cid", reddit_client_secret="csec"))
    posted: list[tuple[str, tuple[str, str], dict[str, str]]] = []

    def fake_post(url: str, *, auth: tuple[str, str], data: dict[str, str], timeout: float):
        posted.append((url, auth, data))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"access_token": "tok123", "token_type": "bearer", "expires_in": 86400},
        )

    source._session.post = fake_post  # type: ignore[method-assign]
    source._ensure_token()
    source._ensure_token()  # cached: no second POST
    assert len(posted) == 1
    url, auth, data = posted[0]
    assert url == "https://www.reddit.com/api/v1/access_token"
    assert auth == ("cid", "csec")
    assert data == {"grant_type": "client_credentials"}
    assert source._token == "tok123"


def test_token_rejected_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    source = RedditSource(Settings(reddit_client_id="bad", reddit_client_secret="bad"))

    def fake_post(url: str, **_: object):
        return SimpleNamespace(
            status_code=401, json=lambda: {"message": "Unauthorized"}
        )

    source._session.post = fake_post  # type: ignore[method-assign]
    with pytest.raises(ConfigurationError) as excinfo:
        source._ensure_token()
    assert "RESEARCHER_REDDIT_CLIENT_ID" in str(excinfo.value)


def test_token_endpoint_429_is_a_rate_limit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    source = RedditSource(Settings(reddit_client_id="cid", reddit_client_secret="csec"))

    def fake_post(url: str, **_: object):
        return SimpleNamespace(status_code=429, json=lambda: {})

    source._session.post = fake_post  # type: ignore[method-assign]
    with pytest.raises(RateLimitError):
        source._ensure_token()


# ---------------------------------------------------------------------- #
# Header rate hints (the _after_response hook)
# ---------------------------------------------------------------------- #


def test_after_response_records_and_feeds_the_rate_headers() -> None:
    source = RedditSource(Settings())
    response = SimpleNamespace(
        headers={
            "X-Ratelimit-Used": "1",
            "X-Ratelimit-Remaining": "0",
            "X-Ratelimit-Reset": "57.0",
        }
    )
    source._after_response(response)
    assert source.last_rate_headers["X-Ratelimit-Remaining"] == "0"
    assert source.last_rate_headers["X-Ratelimit-Reset"] == "57.0"
    assert source._throttle._cooldown_until > 0  # fed into the throttle


def test_after_response_ignores_missing_rate_headers() -> None:
    source = RedditSource(Settings())
    source._after_response(SimpleNamespace(headers={}))
    assert source.last_rate_headers == {"X-Ratelimit-Used": None, "X-Ratelimit-Remaining": None, "X-Ratelimit-Reset": None}
    assert source._throttle._cooldown_until == 0.0


# ---------------------------------------------------------------------- #
# Wrapper meta
# ---------------------------------------------------------------------- #


def test_wrapper_meta_reads_under_the_data_key() -> None:
    source = RedditSource(Settings())
    meta = source._wrapper_meta(load_fixture())
    assert meta["after"] == "t3_xyz789"
    assert meta["before"] is None
    assert meta["dist"] == 2
    assert isinstance(meta, dict)


def test_wrapper_meta_tolerates_non_listing_payloads() -> None:
    source = RedditSource(Settings())
    assert source._wrapper_meta(None) == {}  # type: ignore[arg-type]
    assert source._wrapper_meta({"kind": "Listing"}) == {}


def test_parse_payload_rejects_payloads_without_data(monkeypatch: pytest.MonkeyPatch) -> None:
    source = source_with(monkeypatch, _Capture())
    with pytest.raises(SourceError):
        source.parse_payload({"kind": "Listing", "data": None})
    with pytest.raises(SourceError):
        source.parse_payload([])  # type: ignore[arg-type]