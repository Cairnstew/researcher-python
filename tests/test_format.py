"""Offline tests for the bibliography formatters (pure functions, no network)."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from researcher_python.errors import ExportError
from researcher_python.format import (
    bibtex_entry,
    bibtex_key,
    csl_item,
    render_bibtex,
    render_csl_json,
    render_ris,
    ris_entry,
    to_bibtex,
    to_csl_json,
    to_ris,
)
from researcher_python.models import Author, Paper


def paper(
    title: str,
    *,
    authors: tuple[str, ...] | list[str] = (),
    year: int | None = None,
    doi: str = "",
    arxiv_id: str = "",
    pmid: str = "",
    venue: str = "",
    volume: str = "",
    issue: str = "",
    pages: str = "",
    publisher: str = "",
    work_type: str = "",
    url: str = "",
    abstract: str = "",
    keywords: tuple[str, ...] | list[str] = (),
    published: str = "",
    language: str = "",
    license: str = "",
) -> Paper:
    return Paper(
        title=title,
        authors=[Author(name=a) for a in authors],
        year=year,
        doi=doi,
        arxiv_id=arxiv_id,
        pmid=pmid,
        venue=venue,
        volume=volume,
        issue=issue,
        pages=pages,
        publisher=publisher,
        work_type=work_type,
        url=url,
        abstract=abstract,
        keywords=list(keywords),
        published=published,
        language=language,
        license=license,
    )


# -- BibTeX keys -----------------------------------------------------------


def test_bibtex_key_is_family_year_word() -> None:
    p = paper("Attention Is All You Need", authors=["Ashish Vaswani"], year=2017)
    assert bibtex_key(p) == "vaswani2017attention"


def test_bibtex_key_folds_accents_and_is_ascii() -> None:
    p = paper("\u00dcber Wolken", authors=["J\u00f6rg M\u00fcller"], year=2001)
    assert bibtex_key(p) == "muller2001uber"


def test_bibtex_key_defaults_when_sparse() -> None:
    assert bibtex_key(paper("Lonely Dataset")) == "ndlonely"


# -- BibTeX entries --------------------------------------------------------


@pytest.mark.parametrize(
    ("work_type", "expected"),
    [
        ("journal-article", "@article"),
        ("proceedings-article", "@inproceedings"),
        ("book-chapter", "@incollection"),
        ("monograph", "@book"),
        ("posted-content", "@misc"),
        ("dissertation", "@phdthesis"),
        ("report", "@techreport"),
        ("", "@misc"),
    ],
)
def test_bibtex_entry_type_mapping(work_type: str, expected: str) -> None:
    assert bibtex_entry(paper("T", work_type=work_type)).startswith(expected)


def test_bibtex_authors_are_family_given_and_joined() -> None:
    entry = bibtex_entry(paper("T", authors=["Ashish Vaswani", "Noam Shazeer"]))
    assert "author = {Vaswani, Ashish and Shazeer, Noam}," in entry


def test_bibtex_author_already_in_comma_form_is_kept() -> None:
    assert "author = {Vaswani, Ashish}," in bibtex_entry(
        paper("T", authors=["Vaswani, Ashish"])
    )


def test_bibtex_author_with_a_single_token() -> None:
    assert "author = {Aristotle}," in bibtex_entry(paper("T", authors=["Aristotle"]))


def test_bibtex_omits_fields_the_record_does_not_carry() -> None:
    entry = bibtex_entry(paper("Only a title"))
    assert "title = {Only a title}," in entry
    assert "author" not in entry
    assert "year" not in entry
    assert "doi" not in entry


def test_bibtex_values_cannot_unbalance_braces() -> None:
    entry = bibtex_entry(paper("A {weird} title", authors=["A {B} C"], year=2020))
    assert "title = {A weird title}," in entry
    assert entry.count("{") == entry.count("}")


def test_bibtex_carries_ids_counts_and_pages() -> None:
    entry = bibtex_entry(
        paper(
            "T",
            doi="10.1/x",
            arxiv_id="1706.03762",
            url="https://example.org/x",
            volume="1",
            issue="2",
            pages="10--20",
            publisher="ACM",
            keywords=["a", "b"],
        )
    )
    assert "doi = {10.1/x}," in entry
    assert "eprint = {1706.03762}," in entry
    assert "archivePrefix = {arXiv}," in entry
    assert "volume = {1}," in entry
    assert "number = {2}," in entry
    assert "pages = {10-20}," in entry
    assert "publisher = {ACM}," in entry
    assert "url = {https://example.org/x}," in entry
    assert "keywords = {a, b}," in entry


def test_bibtex_article_uses_journal_and_proceedings_uses_booktitle() -> None:
    article = bibtex_entry(paper("T", venue="J. Things", work_type="journal-article"))
    inproc = bibtex_entry(paper("T", venue="Proc.", work_type="proceedings-article"))
    assert "journal = {J. Things}," in article
    assert "booktitle = {Proc.}," in inproc


# -- whole-document BibTeX ------------------------------------------------


def test_render_bibtex_makes_keys_unique() -> None:
    p = paper("Same title", authors=["A B"], year=2020)
    base = bibtex_key(p)
    text = render_bibtex([p, p, p])
    assert f"@misc{{{base}," in text
    assert f"@misc{{{base}a," in text
    assert f"@misc{{{base}b," in text


def test_render_bibtex_preserves_input_order() -> None:
    text = render_bibtex([paper("Alpha", doi="10.1/a"), paper("Beta", doi="10.1/b")])
    assert text.index("10.1/a") < text.index("10.1/b")


def test_empty_input_renders_cleanly() -> None:
    assert render_bibtex([]) == ""
    assert render_ris([]) == ""
    assert render_csl_json([]) == "[]\n"


# -- RIS -------------------------------------------------------------------


def test_ris_article_record() -> None:
    text = ris_entry(
        paper(
            "A study",
            authors=["Jane Doe", "John Roe"],
            year=1999,
            venue="Journal of Things",
            volume="3",
            issue="4",
            pages="10-20",
            doi="10.1/x",
            work_type="journal-article",
        )
    )
    assert text.startswith("TY  - JOUR\n")
    assert "TI  - A study\n" in text
    assert "AU  - Doe, Jane\n" in text
    assert "AU  - Roe, John\n" in text
    assert "PY  - 1999\n" in text
    assert "JO  - Journal of Things\n" in text
    assert "VL  - 3\n" in text
    assert "IS  - 4\n" in text
    assert "SP  - 10\n" in text
    assert "EP  - 20\n" in text
    assert "DO  - 10.1/x\n" in text
    assert text.rstrip().endswith("ER  -")


def test_ris_book_uses_t2_not_journal() -> None:
    text = ris_entry(paper("B", venue="A Publisher", work_type="book"))
    assert text.startswith("TY  - BOOK\n")
    assert "T2  - A Publisher\n" in text
    assert "JO  -" not in text


def test_ris_single_page_has_no_end_page() -> None:
    text = ris_entry(paper("T", pages="42"))
    assert "SP  - 42\n" in text
    assert "EP  -" not in text


def test_ris_flattens_newlines_in_fields() -> None:
    text = ris_entry(paper("Line one\nLine two", abstract="a\n\nb"))
    assert "TI  - Line one Line two\n" in text
    assert "AB  - a b\n" in text


# -- CSL-JSON --------------------------------------------------------------


def test_csl_item_uses_the_standard_author_shape() -> None:
    item = csl_item(
        paper(
            "Title",
            authors=["Jane Doe"],
            year=2020,
            doi="10.1/x",
            venue="Venue",
            pages="1-2",
            work_type="journal-article",
        )
    )
    assert item["id"] == "10.1/x"
    assert item["type"] == "article-journal"
    assert item["author"] == [{"family": "Doe", "given": "Jane"}]
    assert item["issued"] == {"date-parts": [[2020]]}
    assert item["container-title"] == "Venue"
    assert item["page"] == "1-2"
    assert item["DOI"] == "10.1/x"
    # The bibify wire shape must not leak in: no "full"/"first"/"last".
    assert "full" not in item["author"][0]


def test_csl_single_token_author_becomes_a_literal() -> None:
    assert csl_item(paper("T", authors=["Aristotle"]))["author"] == [
        {"literal": "Aristotle"}
    ]


def test_csl_issued_falls_back_to_a_literal_without_a_year() -> None:
    assert csl_item(paper("T", published="2020-05"))["issued"] == {"literal": "2020-05"}


def test_csl_sparse_item_carries_only_what_it_has() -> None:
    assert set(csl_item(paper("T"))) == {"id", "type", "title"}


@pytest.mark.parametrize(
    ("work_type", "expected"),
    [
        ("journal-article", "article-journal"),
        ("proceedings-article", "paper-conference"),
        ("book-chapter", "chapter"),
        ("posted-content", "article"),
        ("dataset", "dataset"),
        ("", "document"),
    ],
)
def test_csl_type_mapping(work_type: str, expected: str) -> None:
    assert csl_item(paper("T", work_type=work_type))["type"] == expected


def test_render_csl_json_round_trips() -> None:
    items = json.loads(
        render_csl_json([paper("A", doi="10.1/a"), paper("B", doi="10.1/b")])
    )
    assert [item["title"] for item in items] == ["A", "B"]


# -- writing ---------------------------------------------------------------


def test_to_bibtex_writes_a_path(tmp_path: Any) -> None:
    dest = tmp_path / "refs.bib"
    to_bibtex([paper("A", doi="10.1/a")], dest)
    assert dest.read_text(encoding="utf-8").startswith("@misc{")


def test_to_ris_and_to_csl_json_write_paths(tmp_path: Any) -> None:
    to_ris([paper("A")], tmp_path / "refs.ris")
    to_csl_json([paper("A")], tmp_path / "refs.json")
    assert (tmp_path / "refs.ris").read_text(encoding="utf-8").startswith("TY  - ")
    assert json.loads((tmp_path / "refs.json").read_text(encoding="utf-8"))[0][
        "title"
    ] == "A"


def test_writers_accept_an_open_stream() -> None:
    buffer = io.StringIO()
    to_bibtex([paper("A", doi="10.1/a")], buffer)
    assert "@misc{" in buffer.getvalue()


def test_writer_rejects_a_non_path_non_stream() -> None:
    with pytest.raises(ExportError):
        to_bibtex([paper("A")], 123)  # type: ignore[arg-type]


# -- determinism -----------------------------------------------------------


def test_rendering_is_deterministic() -> None:
    p = paper("Title", authors=["Jane Doe"], year=2020, doi="10.1/x")
    assert render_bibtex([p]) == render_bibtex([p])
    assert render_ris([p]) == render_ris([p])
    assert render_csl_json([p]) == render_csl_json([p])


def test_doi_case_is_preserved_in_output() -> None:
    p = paper("T", doi="10.1103/PhysRevLett.116.061102")
    assert "10.1103/PhysRevLett.116.061102" in bibtex_entry(p)
    assert csl_item(p)["DOI"] == "10.1103/PhysRevLett.116.061102"
