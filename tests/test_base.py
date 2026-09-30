"""Tests for :mod:`researcher_python.sources.base` — shared adapter machinery."""

from __future__ import annotations

import threading
import time

import pytest

from researcher_python.errors import SourceUnavailableError, UnsupportedOperation
from researcher_python.models import Paper
from researcher_python.sources.base import (
    Capabilities,
    RateGate,
    Source,
    parse_retry_after,
    require_dependency,
    squash_text,
)


# -- squash_text -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", "plain"),
        ("  leading and trailing  ", "leading and trailing"),
        ("multi\nline\ttext", "multi line text"),
        ("a    b", "a b"),
        ("", ""),
        (None, ""),
        ("  ", ""),
        (123, "123"),  # non-strings are stringified
        (0, "0"),
    ],
)
def test_squash_text(value: object, expected: str) -> None:
    assert squash_text(value) == expected


def test_squash_text_is_idempotent() -> None:
    once = squash_text("a  b\t c")
    assert squash_text(once) == once


# -- Capabilities ----------------------------------------------------------


def test_capability_defaults() -> None:
    caps = Capabilities()
    assert caps.search is True
    assert caps.get is True
    assert caps.references is False
    assert caps.citations is False
    assert caps.pdf is False


def test_supports_only_reports_declared_operations() -> None:
    caps = Capabilities(search=True, get=True, references=False, citations=False, pdf=True)
    assert caps.supports("search") is True
    assert caps.supports("pdf") is True
    assert caps.supports("references") is False
    assert caps.supports("nonsense") is False  # unknown operations are never implied


# -- require_dependency ----------------------------------------------------


def test_require_dependency_returns_an_importable_module() -> None:
    module = require_dependency("json", "demo")
    assert module.__name__ == "json"


def test_require_dependency_names_the_source_when_missing() -> None:
    with pytest.raises(SourceUnavailableError) as excinfo:
        require_dependency("definitely_not_a_module_xyz", "pubmed")
    assert excinfo.value.source == "pubmed"
    assert "definitely_not_a_module_xyz" in str(excinfo.value)
    assert "not installed" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ImportError)


# -- RateGate --------------------------------------------------------------


def test_first_wait_returns_immediately() -> None:
    gate = RateGate(min_interval=0.05)
    assert gate.wait() == 0.0


def test_second_wait_pays_the_interval() -> None:
    gate = RateGate(min_interval=0.05)
    gate.wait()
    started = time.monotonic()
    gate.wait()
    assert time.monotonic() - started >= 0.045


def test_zero_interval_still_serialises() -> None:
    """A 0 interval still takes the lock, so concurrent callers cannot race."""
    gate = RateGate(min_interval=0.0)
    delays = [gate.wait() for _ in range(100)]
    assert delays == [0.0] * 100


def test_min_interval_is_clamped_to_non_negative() -> None:
    assert RateGate(min_interval=-5).min_interval == 0.0


def test_rate_gate_serialises_across_threads() -> None:
    """N threads × K waits must be spaced by min_interval, not run together."""
    interval = 0.01
    threads, workers, per_thread = 8, 8, 3
    gate = RateGate(min_interval=interval)
    started = threading.Event()

    def worker() -> None:
        started.wait()
        for _ in range(per_thread):
            gate.wait()

    pool = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in pool:
        thread.start()
    began = time.monotonic()
    started.set()
    for thread in pool:
        thread.join()

    total = workers * per_thread
    elapsed = time.monotonic() - began
    # If the lock were broken the runs would overlap and elapsed would be ~0.
    assert elapsed >= (total - 1) * interval * 0.8


# -- Source contract -------------------------------------------------------


class _ExampleSource(Source):
    name = "example"
    capabilities = Capabilities(
        search=True, get=True, references=False, citations=False, pdf=False
    )

    def search(self, query: str, *, limit: int | None = None) -> list[Paper]:
        return []

    def get(self, key: str) -> Paper:
        raise NotImplementedError


def test_source_support_and_require_support() -> None:
    source = _ExampleSource()
    assert source.supports("search") is True
    assert source.supports("references") is False

    source.require_support("search")  # no-op
    with pytest.raises(UnsupportedOperation) as excinfo:
        source.require_support("references")
    assert "'example'" in str(excinfo.value)
    assert "references" in str(excinfo.value)


def test_optional_operations_refuse_through_require_support() -> None:
    source = _ExampleSource()
    with pytest.raises(UnsupportedOperation):
        source.references(Paper())
    with pytest.raises(UnsupportedOperation):
        source.citations(Paper())


def test_source_repr_names_the_class() -> None:
    assert "ExampleSource" in repr(_ExampleSource())


# -- parse_retry_after ------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "7"}, 7.0),
        ({"Retry-After": "0"}, 0.0),
        ({"Retry-After": "2.5"}, 2.5),
        ({"Retry-After": "-3"}, 0.0),  # clamped, never negative
        ({}, None),  # header absent
        (None, None),
        ({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, None),  # HTTP-date form
        ({"Retry-After": "soon"}, None),  # garbage
        ({"Retry-After": ""}, None),
        ({"Retry-After": None}, None),
    ],
)
def test_parse_retry_after(headers: Any, expected: float | None) -> None:
    assert parse_retry_after(headers) == expected


def test_parse_retry_after_is_importable_from_the_public_base_surface() -> None:
    assert "parse_retry_after" in __import__(
        "researcher_python.sources.base", fromlist=["__all__"]
    ).__all__
