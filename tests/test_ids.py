"""Table-driven tests for :mod:`researcher_python.ids`.

Every normaliser must return ``""`` / ``None`` / ``False`` for garbage rather
than raising, so the negative cases are as important as the positive ones.
"""

from __future__ import annotations

import pytest

from researcher_python.ids import (
    is_arxiv_id,
    is_doi,
    is_pmid,
    normalize_arxiv_id,
    normalize_doi,
    normalize_orcid,
    normalize_pmid,
    normalize_title,
    wire_doi,
    year_from_date,
)

# -- DOI -------------------------------------------------------------------

MIXED_DOI = "10.1103/PhysRevLett.116.061102"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10.1000/xyz", "10.1000/xyz"),
        ("10.1000/XYZ", "10.1000/xyz"),  # D3: lowercased for the dedupe key
        (f"https://doi.org/{MIXED_DOI}", MIXED_DOI.lower()),
        (f"doi:{MIXED_DOI}", MIXED_DOI.lower()),
        ("https://dx.doi.org/10.1000/xyz", "10.1000/xyz"),
        ("10.1000/xyz.", "10.1000/xyz"),  # trailing citation punctuation
        ("10.1000/xyz;", "10.1000/xyz"),
        ("  10.1000/xyz  ", "10.1000/xyz"),
        ("not-a-doi", ""),
        ("22470460", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_doi(value: str | None, expected: str) -> None:
    assert normalize_doi(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10.1000/xyz", True),
        (f"https://doi.org/{MIXED_DOI}", True),
        (f"doi:{MIXED_DOI}", True),
        ("nope", False),
        ("", False),
        (None, False),
    ],
)
def test_is_doi(value: str | None, expected: bool) -> None:
    assert is_doi(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # D3: the wire keeps the caller's casing, the key above does not.
        (MIXED_DOI, MIXED_DOI),
        (f"https://doi.org/{MIXED_DOI}", MIXED_DOI),
        (f"doi:{MIXED_DOI}", MIXED_DOI),
        ("10.1038/nature14539.", "10.1038/nature14539"),
        ("arXiv:1706.03762", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_wire_doi(value: str | None, expected: str) -> None:
    assert wire_doi(value) == expected


# -- arXiv -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "keep_version", "expected"),
    [
        ("1706.03762", False, "1706.03762"),
        ("1706.03762v7", False, "1706.03762"),
        ("1706.03762v7", True, "1706.03762v7"),
        ("arXiv:1706.03762", False, "1706.03762"),
        ("https://arxiv.org/abs/1706.03762", False, "1706.03762"),
        ("https://arxiv.org/pdf/1706.03762v2.pdf", False, "1706.03762"),
        # Legacy (pre-2007) identifiers keep their archive prefix.
        ("hep-th/9901001", False, "hep-th/9901001"),
        ("hep-th/9901001v3", False, "hep-th/9901001"),
        ("arxiv:hep-th/9901001", False, "hep-th/9901001"),
        # Not an id: returned stripped rather than raised on.
        ("not an arxiv id", False, "not an arxiv id"),
        ("", False, ""),
        (None, False, ""),
    ],
)
def test_normalize_arxiv_id(value: str | None, keep_version: bool, expected: str) -> None:
    assert normalize_arxiv_id(value, keep_version=keep_version) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1706.03762", True),
        ("1706.03762v7", True),
        ("https://arxiv.org/abs/1706.03762", True),
        ("hep-th/9901001", True),
        ("not an arxiv id", False),
        ("10.1000/xyz", False),
        ("", False),
        (None, False),
    ],
)
def test_is_arxiv_id(value: str | None, expected: bool) -> None:
    assert is_arxiv_id(value) is expected


# -- PMID ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("12345678", "12345678"),
        ("https://pubmed.ncbi.nlm.nih.gov/12345678/", "12345678"),
        ("https://www.ncbi.nlm.nih.gov/pubmed/12345678", "12345678"),
        ("pmid:12345678", "12345678"),
        ("PMID:12345678", "12345678"),
        ("  22470460  ", "22470460"),
        ("not-a-pmid", ""),
        ("10.1000/xyz", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_pmid(value: str | None, expected: str) -> None:
    assert normalize_pmid(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("12345678", True), ("https://pubmed.ncbi.nlm.nih.gov/1/", True), ("nope", False), (None, False)],
)
def test_is_pmid(value: str | None, expected: bool) -> None:
    assert is_pmid(value) is expected


# -- titles ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Hello, World!", "hello world"),
        ("A  B", "a b"),
        ("A & B", "a and b"),
        ("Café Déjà Vu!", "café déjà vu"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_title(value: str | None, expected: str) -> None:
    assert normalize_title(value) == expected


def test_normalize_title_folds_for_dedupe() -> None:
    assert normalize_title("Deep Learning!") == normalize_title("deep learning")


# -- ORCID -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://orcid.org/0000-0002-1825-0097", "0000-0002-1825-0097"),
        ("orcid:0000-0002-1825-0097", "0000-0002-1825-0097"),
        ("0000-0002-1825-0097", "0000-0002-1825-0097"),
        # Checksum digit may be X (lower-case input: see the xfail below).
        ("https://orcid.org/0000-0002-1825-009X", "0000-0002-1825-009X"),
        ("not-an-orcid", ""),
        ("0000-0002-1825", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_orcid(value: str | None, expected: str) -> None:
    assert normalize_orcid(value) == expected


def test_normalize_orcid_accepts_lowercase_checksum() -> None:
    assert normalize_orcid("0000-0002-1825-009x") == "0000-0002-1825-009X"


# -- years -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2021-04-15", 2021),
        ("2021-04", 2021),
        ("2021", 2021),
        ("2003 Jan-Feb", 2003),  # MEDLINE form
        ("2019 Dec-2020 Jan", 2019),  # first year wins
        ("", None),
        (None, None),
        ("no year here", None),
        ("0999-01-01", None),  # outside the sane range
    ],
)
def test_year_from_date(value: str | None, expected: int | None) -> None:
    assert year_from_date(value) == expected


def test_year_from_date_does_not_mine_ids_or_pages() -> None:
    # A bare identifier must not be mistaken for a year.
    assert year_from_date("10.1000/xyz") is None


