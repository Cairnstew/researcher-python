"""One canonical work-type vocabulary across five spellings."""

from __future__ import annotations

import pytest

from researcher_python.work_types import (
    CANONICAL_WORK_TYPES,
    DEFAULT_SEARCH_EXCLUDE_TYPES,
    normalize_work_type,
    resolve_types,
)


@pytest.mark.parametrize(
    ("native", "canonical"),
    [
        ("article", "article"),
        ("Journal Article", "article"),
        ("journal-article", "article"),
        ("posted-content", "preprint"),
        ("Preprint", "preprint"),
        ("book-chapter", "book-chapter"),
        ("Book Section", "book-chapter"),
        ("conference-paper", "proceedings-article"),
        ("Conference", "proceedings-article"),
        ("dissertation", "thesis"),
        ("JournalArticle", "article"),
        ("LettersAndComments", "letter"),
        ("grant", "grant"),
        ("standard-series", "standard"),
        ("dataset", "dataset"),
    ],
)
def test_native_spellings_normalise(native: str, canonical: str) -> None:
    assert normalize_work_type(native) == canonical


def test_unknown_is_empty_not_a_guess() -> None:
    assert normalize_work_type("mystery-object") == ""
    assert normalize_work_type("") == ""
    assert normalize_work_type(None) == ""


def test_resolve_accepts_aliases_and_deduplicates() -> None:
    assert resolve_types(("journal-article", "article", "Review")) == (
        "article",
        "review",
    )
    assert resolve_types("article,review") == ("article", "review")


def test_resolve_rejects_a_typo_and_names_the_legal_values() -> None:
    with pytest.raises(ValueError, match="unknown work type"):
        resolve_types(("artcle",))


def test_default_exclusions_are_grants_and_standards_only() -> None:
    # Datasets are deliberately not excluded: they can be legitimate hits and
    # prescreen flags them later, visibly.
    assert DEFAULT_SEARCH_EXCLUDE_TYPES == ("grant", "standard")
    assert "dataset" in CANONICAL_WORK_TYPES
    assert "dataset" not in DEFAULT_SEARCH_EXCLUDE_TYPES
