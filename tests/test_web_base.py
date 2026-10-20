"""Offline tests for the web-source framework: shared machinery, registry.

Everything here runs without network: parsers are pure, ``_get`` is stubbed,
and sleeps go through ``web_sources.base.time`` / ``web_sources._sleep``
seams so a test never waits.
"""

from __future__ import annotations

import json
import time as _time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from researcher_python.config import Settings
from researcher_python.errors import ConfigurationError, RateLimitError, SourceError
from researcher_python.web_sources import (
    DEFAULT_WEB_SOURCES,
    WEB_SOURCES,
    create_web_source,
    web_search,
    web_source_names,
)
from researcher_python.web_sources import base as web_base
from researcher_python.web_sources.base import (
    WebResult,
    WebSearchResult,
    WebSource,
    WebThrottle,
    classify_web_error,
)
from researcher_python.web_sources.stackexchange import StackExchangeSource

_FIXTURE = Path(__file__).parent / "fixtures" / "stackexchange_search.json"

#: ``time.sleep`` calls recorded, cleared per test.
SLEEPS: list[float] = []


def load_fixture() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text())


@pytest.fixture(autouse=True)
def _clear_sleeps() -> None:
    SLEEPS.clear()


def stub_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send every sleep through SLEEPS; keep monotonic real."""
    monkeypatch.setattr(
        web_base,
        "time",
        SimpleNamespace(sleep=SLEEPS.append, monotonic=_time.monotonic),
    )


# ---------------------------------------------------------------------- #
# WebResult / WebSearchResult
# ---------------------------------------------------------------------- #


def test_web_result_round_trips_through_dict() -> None:
    record = WebResult(
        source="stackoverflow",
        id="5946236",
        title="How to merge dicts?",
        url="https://stackoverflow.com/questions/5946236",
        score=165,
        created=1305010204,
        author="Salil",
        tags=["python", "dictionary"],
        extra={"answer_count": 13, "is_answered": True},
    )
    restored = WebResult.from_dict(record.to_dict())
    assert restored == record


def test_web_result_from_dict_ignores_unknown_keys() -> None:
    restored = WebResult.from_dict({"source": "x", "id": "1", "shiny": "new"})
    assert restored.source == "x"
    assert restored.id == "1"
    assert restored.extra == {}


def test_web_search_result_round_trips() -> None:
    result = WebSearchResult(
        results=[WebResult(id="1", source="stackoverflow")],
        source="stackoverflow",
        truncated=True,
        pages=2,
        quota={"max": 300, "remaining": 250},
    )
    view = result.to_dict()
    assert view["pages"] == 2
    assert view["truncated"] is True
    assert view["quota"] == {"max": 300, "remaining": 250}
    assert view["results"][0]["id"] == "1"


# ---------------------------------------------------------------------- #
# WebThrottle
# ---------------------------------------------------------------------- #


def test_throttle_first_request_does_not_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_time(monkeypatch)
    throttle = WebThrottle(min_interval=2.0)
    throttle.wait()
    assert SLEEPS == []


def test_throttle_waits_when_last_request_was_recent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_time(monkeypatch)
    throttle = WebThrottle(min_interval=2.0)
    throttle._last = _time.monotonic()  # last request "now"
    throttle.wait()
    assert SLEEPS, "expected a sleep to enforce the interval"
    assert SLEEPS[0] >= 1.9


def test_throttle_waits_out_a_backoff_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_time(monkeypatch)
    throttle = WebThrottle(min_interval=0.0)
    throttle.note_backoff(5)
    throttle.wait()
    assert SLEEPS and SLEEPS[0] >= 4.9


def test_throttle_backoff_ignores_garbage() -> None:
    throttle = WebThrottle(min_interval=0.0)
    throttle.note_backoff(None)
    throttle.note_backoff(-3)
    throttle.note_backoff(True)
    assert throttle._cooldown_until == 0.0


# ---------------------------------------------------------------------- #
# Error classification
# ---------------------------------------------------------------------- #


def test_throttle_violation_maps_to_rate_limit() -> None:
    exc = classify_web_error(
        "stackoverflow",
        {
            "error_id": 502,
            "error_message": "throttle_violation",
            "error_name": "throttle_violation",
        },
        400,
    )
    assert isinstance(exc, RateLimitError)
    assert exc.source == "stackoverflow"
    assert "throttle_violation" in str(exc)


def test_http_429_maps_to_rate_limit_even_without_a_body() -> None:
    exc = classify_web_error("stackoverflow", None, 429)
    assert isinstance(exc, RateLimitError)


def test_bad_parameter_maps_to_source_error_with_the_api_message() -> None:
    exc = classify_web_error(
        "stackoverflow",
        {"error_id": 400, "error_message": "invalid sort", "error_name": "bad_parameter"},
        400,
    )
    assert isinstance(exc, SourceError)
    assert "invalid sort" in str(exc)


def test_temporarily_unavailable_maps_to_source_error() -> None:
    exc = classify_web_error(
        "stackoverflow",
        {
            "error_id": 503,
            "error_message": "temporarily_unavailable",
            "error_name": "temporarily_unavailable",
        },
        400,
    )
    assert isinstance(exc, SourceError)
    assert not isinstance(exc, RateLimitError)


def test_plain_status_falls_back_to_a_message() -> None:
    exc = classify_web_error("stackoverflow", None, 500)
    assert isinstance(exc, SourceError)
    assert "HTTP 500" in str(exc)


# ---------------------------------------------------------------------- #
# Registry
# ---------------------------------------------------------------------- #


def test_stackoverflow_is_registered_and_first() -> None:
    assert "stackoverflow" in WEB_SOURCES
    assert web_source_names() == list(WEB_SOURCES)
    assert web_source_names()[0] == "stackoverflow"
    assert DEFAULT_WEB_SOURCES == ("stackoverflow",)


def test_unknown_web_source_raises_configuration_error() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        create_web_source("definitely_not_a_site")
    assert "definitely_not_a_site" in str(excinfo.value)
    assert "stackoverflow" in str(excinfo.value)


def test_create_web_source_passes_settings_and_kwargs() -> None:
    settings = Settings(stackexchange_key="sek", max_results=7)
    source = create_web_source("stackoverflow", settings, site="superuser")
    assert source.settings is settings
    assert source.settings.max_results == 7
    assert isinstance(source, StackExchangeSource)
    assert source.site == "superuser"


def test_create_web_source_defaults_site_to_stackoverflow() -> None:
    source = create_web_source("stackoverflow")
    assert isinstance(source, StackExchangeSource)
    assert source.site == "stackoverflow"


# ---------------------------------------------------------------------- #
# web_search entry point + retry
# ---------------------------------------------------------------------- #


class _FlakySource(WebSource):
    """Search fails once with RateLimitError, then succeeds."""

    name = "flaky"
    base_url = "https://example.invalid"
    endpoint = "/x"
    sort_modes = ("relevance",)
    default_interval = 0.0

    def __init__(self, settings: Settings | None = None, **_: object) -> None:
        super().__init__(settings)
        self.calls = 0

    def query_params(self, query, sort, page, pagesize):  # noqa: ANN001
        return {}

    def parse_payload(self, payload: Any):
        return [], False, {}

    def search(self, query, *, max_results=None, sort=None) -> WebSearchResult:  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            raise RateLimitError(self.name, "throttle_violation")
        return WebSearchResult(results=[], source=self.name, pages=0)


def test_web_search_retries_a_rate_limit_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import researcher_python.web_sources as web_init

    source = _FlakySource(Settings())
    monkeypatch.setattr(web_init, "create_web_source", lambda name, settings=None, **kw: source)
    monkeypatch.setattr(web_init, "_sleep", SLEEPS.append)

    result = web_search("anything", settings=Settings(retries=1))
    assert result.source == "flaky"
    assert source.calls == 2  # first attempt throttled, second succeeded
    assert SLEEPS, "expected a sleep between the two attempts"


def test_web_search_stops_after_retries_are_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import researcher_python.web_sources as web_init

    calls = {"n": 0}

    def sticky_search(self, query, *, max_results=None, sort=None):  # noqa: ANN001
        calls["n"] += 1
        raise RateLimitError(self.name, "throttle_violation")

    monkeypatch.setattr(
        web_init,
        "create_web_source",
        lambda name, settings=None, **kw: _FlakySource(settings),
    )
    monkeypatch.setattr(web_init, "_sleep", SLEEPS.append)
    monkeypatch.setattr(_FlakySource, "search", sticky_search)

    with pytest.raises(RateLimitError):
        web_search("anything", settings=Settings(retries=2))
    assert calls["n"] == 3  # initial + 2 retries


def test_web_search_unknown_source_raises_without_any_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import researcher_python.web_sources as web_init

    monkeypatch.setattr(
        web_init,
        "create_web_source",
        lambda name, settings=None, **kw: (_ for _ in ()).throw(
            ConfigurationError(f"unknown web source {name!r}")
        ),
    )

    with pytest.raises(ConfigurationError):
        web_search("x")