"""Harvard (Cite Them Right) rendering — offline, pure functions only."""

from __future__ import annotations

import io

import pytest

from researcher_python import format as bib
from researcher_python.models import Author, Paper


def _paper(**kwargs) -> Paper:
    return Paper(**kwargs)


ARTICLE = _paper(
    doi="10.1016/j.ai.2018.04.008",
    year=2018,
    title="AI4People: An Ethical Framework for a Good AI Society",
    authors=[Author("Floridi, Luciano"), Author("Cowls, Josh")],
    venue="Minds and Machines",
    volume="28",
    issue="3",
    pages="754-800",
    work_type="journal-article",
)

BOOK = _paper(
    isbn="978-3-031-24863-4",
    year=2023,
    title="Ethical and Social Issues in the Information Age",
    authors=[Author("Kizza, Joseph Migga")],
    publisher="Springer",
    work_type="book",
)

REPORT = _paper(
    year=2024,
    title="Governing AI for Humanity",
    # An organisation must be flagged: no source can be trusted to know it.
    authors=[Author("United Nations", organization=True)],
    publisher="United Nations",
    work_type="report",
    url="https://un.org/governing-ai",
)

CONFERENCE = _paper(
    arxiv_id="1706.03762",
    year=2017,
    title="Attention Is All You Need",
    authors=[Author("Vaswani, Ashish")],
    venue="NeurIPS",
    pages="1-12",
    work_type="conference-paper",
)

NO_YEAR = _paper(title="Undated work", authors=[Author("Solo, Sam")])
NO_AUTHOR = _paper(year=2020, title="Anonymous report on something")


class TestEntryShape:
    def test_article_is_author_year_title_venue_volume_issue_pages(self) -> None:
        entry = bib.harvard_entry(ARTICLE)
        assert entry.startswith("Floridi, L. and Cowls, J. (2018) ")
        assert "Minds and Machines, 28(3), pp. 754-800" in entry
        assert entry.endswith("doi: 10.1016/j.ai.2018.04.008.")

    def test_book_uses_publisher_and_no_fabricated_place(self) -> None:
        entry = bib.harvard_entry(BOOK)
        assert entry == (
            "Kizza, J.M. (2023) Ethical and Social Issues in the Information Age. "
            "Springer."
        )

    def test_thesis_carries_the_degree_marker(self) -> None:
        entry = bib.harvard_entry(
            _paper(
                year=2022,
                title="A thesis",
                authors=[Author("Doe, Jane")],
                venue="University of Glasgow",
                work_type="dissertation",
            )
        )
        assert "[Doctoral thesis, University of Glasgow]" in entry

    def test_conference_is_an_In_entry(self) -> None:
        assert "In: NeurIPS, pp. 1-12" in bib.harvard_entry(CONFERENCE)

    def test_conference_without_a_container_does_not_emit_a_dangling_In(self) -> None:
        entry = bib.harvard_entry(
            _paper(year=2017, title="Bare", authors=[Author("V, A")],
                   work_type="proceedings-article")
        )
        assert "In:" not in entry
        assert entry.endswith(".")

    def test_missing_year_is_nd(self) -> None:
        assert "(n.d.)" in bib.harvard_entry(NO_YEAR)

    def test_report_gets_an_Available_at_line_with_the_accessed_date(self) -> None:
        entry = bib.harvard_entry(REPORT, accessed="22 October 2026")
        assert "Available at: https://un.org/governing-ai (Accessed: 22 October 2026)" in entry

    def test_accessed_is_omitted_by_default_so_bytes_are_stable(self) -> None:
        assert "Available at" in bib.harvard_entry(REPORT)
        assert "Accessed" not in bib.harvard_entry(REPORT)

    def test_no_double_period_before_the_doi_tail(self) -> None:
        entry = bib.harvard_entry(ARTICLE)
        assert ".." not in entry


class TestAuthorFormatting:
    def test_two_authors_join_with_and(self) -> None:
        assert "Floridi, L. and Cowls, J." in bib.harvard_entry(ARTICLE)

    def test_three_authors_list_all_three(self) -> None:
        entry = bib.harvard_entry(
            _paper(year=2020, title="T", authors=[Author("A, An"), Author("B, Bo"), Author("C, Cy")])
        )
        assert "A, A., B, B. and C, C. (2020)" in entry

    def test_four_authors_collapse_to_three_plus_et_al(self) -> None:
        entry = bib.harvard_entry(
            _paper(
                year=2019,
                title="Four",
                authors=[Author("A, An"), Author("B, Bo"), Author("C, Cy"), Author("D, Di")],
            )
        )
        assert "A, A., B, B. and C, C. et al. (2019)" in entry

    def test_initials_are_unspaced_and_dotted(self) -> None:
        entry = bib.harvard_entry(_paper(year=2020, title="X", authors=[Author("Smith, John Ronald")]))
        assert "Smith, J.R. (2020)" in entry

    def test_hyphenated_forename_keeps_its_hyphen(self) -> None:
        entry = bib.harvard_entry(_paper(year=2020, title="X", authors=[Author("Martin, Jean-Paul")]))
        assert "Martin, J.-P." in entry

    def test_corporate_author_is_not_split_into_initials(self) -> None:
        entry = bib.harvard_entry(REPORT)
        assert "United Nations (2024)" in entry
        assert "Nations, U." not in entry

    def test_corporate_author_keeps_its_name_in_csl(self) -> None:
        item = bib.csl_item(REPORT)
        assert item["author"] == [{"literal": "United Nations"}]


class TestReferenceList:
    def test_is_ordered_alphabetically_by_first_author(self) -> None:
        listing = bib.render_harvard([CONFERENCE, ARTICLE, BOOK])
        assert listing.index("Floridi") < listing.index("Kizza") < listing.index("Vaswani")

    def test_entries_are_separated_by_a_blank_line(self) -> None:
        listing = bib.render_harvard([ARTICLE, BOOK])
        assert "\n\n" in listing
        assert len(listing.strip().split("\n\n")) == 2

    def test_sort_key_handles_missing_years_and_authors(self) -> None:
        assert bib.harvard_author_sort_key(NO_YEAR) == ("solo", 0, "undated work")
        assert bib.harvard_author_sort_key(NO_AUTHOR) == ("", 2020, "anonymous report on something")

    def test_rendering_is_a_pure_function_of_the_input(self) -> None:
        first = bib.render_harvard([ARTICLE, BOOK, REPORT])
        second = bib.render_harvard([ARTICLE, BOOK, REPORT])
        assert first == second

    def test_empty_input_renders_empty(self) -> None:
        assert bib.render_harvard([]) == ""


class TestInTextCitation:
    def test_one_author(self) -> None:
        assert bib.harvard_citation(NO_YEAR) == "(Solo, n.d.)"

    def test_two_authors_use_and(self) -> None:
        assert bib.harvard_citation(ARTICLE) == "(Floridi and Cowls, 2018)"

    def test_four_or_more_use_et_al(self) -> None:
        four = _paper(
            year=2019, title="F",
            authors=[Author("A, An"), Author("B, Bo"), Author("C, Cy"), Author("D, Di")],
        )
        assert bib.harvard_citation(four) == "(A et al., 2019)"

    def test_three_authors_list_all_three(self) -> None:
        three = _paper(year=2020, title="T", authors=[Author("A, An"), Author("B, Bo"), Author("C, Cy")])
        assert bib.harvard_citation(three) == "(A, B and C, 2020)"

    def test_surnames_keep_their_capitalisation(self) -> None:
        assert bib.harvard_citation(ARTICLE).startswith("(Floridi and Cowls,")

    def test_no_author_cites_a_shortened_title(self) -> None:
        long_title = _paper(year=2020, title="An ethical framework for a good society")
        assert bib.harvard_citation(long_title) == (
            "('An ethical framework for', 2020)"
        )
        # A title of four words or fewer is already short enough.
        assert bib.harvard_citation(NO_AUTHOR).startswith("('Anonymous")

    def test_citation_map_is_keyed_by_stable_identifier(self) -> None:
        mapping = bib.harvard_citation_map([ARTICLE, BOOK])
        assert mapping["10.1016/j.ai.2018.04.008"] == "(Floridi and Cowls, 2018)"
        assert mapping["978-3-031-24863-4"] == "(Kizza, 2023)"


class TestWriter:
    def test_writes_to_a_file_object(self) -> None:
        buffer = io.StringIO()
        bib.to_harvard([ARTICLE], buffer, accessed="22 October 2026")
        assert "Floridi" in buffer.getvalue()

    def test_writes_to_a_path(self, tmp_path) -> None:
        target = tmp_path / "refs.txt"
        bib.to_harvard([ARTICLE], target)
        assert target.read_text().startswith("Floridi")

    def test_unwritable_path_raises_export_error(self, tmp_path) -> None:
        from researcher_python.errors import ExportError

        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        with pytest.raises(ExportError):
            bib.to_harvard([ARTICLE], blocker / "refs.txt")