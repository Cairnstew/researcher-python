"""Tests for :mod:`researcher_python.errors` — the hierarchy and its contracts."""

from __future__ import annotations

import pytest

from researcher_python.errors import (
    ConfigurationError,
    DownloadError,
    ExportError,
    NoPdfError,
    NotFoundError,
    RateLimitError,
    ResearcherError,
    SourceError,
    SourceUnavailableError,
    UnsupportedOperation,
)


def test_every_error_derives_from_researcher_error() -> None:
    for cls in (
        ConfigurationError,
        SourceUnavailableError,
        SourceError,
        NotFoundError,
        RateLimitError,
        UnsupportedOperation,
        ExportError,
        DownloadError,
        NoPdfError,
    ):
        assert issubclass(cls, ResearcherError), cls.__name__


def test_source_failures_form_a_chain() -> None:
    assert issubclass(NotFoundError, SourceError)
    assert issubclass(RateLimitError, SourceError)
    assert issubclass(NoPdfError, DownloadError)
    assert issubclass(DownloadError, ExportError) is False  # separate branches


def test_unrelated_errors_stay_unrelated() -> None:
    assert not issubclass(ConfigurationError, SourceError)
    assert not issubclass(NotFoundError, RateLimitError)
    assert not issubclass(NoPdfError, ExportError)
    assert not issubclass(SourceUnavailableError, SourceError)


def test_source_error_keeps_source_message_and_cause() -> None:
    cause = ValueError("root cause")
    error = SourceError("arxiv", "the request failed", cause=cause)

    assert error.source == "arxiv"
    assert str(error) == "the request failed"
    assert error.cause is cause
    assert error.__cause__ is cause


def test_source_error_default_message_names_the_source() -> None:
    assert str(SourceError("crossref")) == "crossref: request failed"


def test_not_found_and_rate_limit_carry_the_source_name() -> None:
    for cls in (NotFoundError, RateLimitError):
        error = cls("pubmed", "nope")
        assert error.source == "pubmed"
        assert str(error) == "nope"
        assert isinstance(error, SourceError)


def test_source_unavailable_exposes_source_and_reason() -> None:
    error = SourceUnavailableError("pubmed", "dependency 'Bio' is not installed")
    assert error.source == "pubmed"
    assert error.reason == "dependency 'Bio' is not installed"
    assert "source 'pubmed' is unavailable" in str(error)


def test_export_error_accepts_a_cause() -> None:
    cause = OSError("disk full")
    error = ExportError("could not write out.json", cause=cause)
    assert str(error) == "could not write out.json"
    assert error.cause is cause
    assert error.__cause__ is cause


def test_rate_limit_error_carries_retry_after() -> None:
    # Absent means "the server said nothing" -> pipeline falls back to 2s.
    assert RateLimitError("crossref", "throttled").retry_after is None

    error = RateLimitError("crossref", "throttled", retry_after=30.0)
    assert error.retry_after == 30.0
    assert error.source == "crossref"
    assert str(error) == "throttled"
    assert isinstance(error, SourceError)
