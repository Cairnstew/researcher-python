"""Literature / synthesis matrices and screening state — offline."""

from __future__ import annotations

import csv
import io

import pytest

from researcher_python import matrix
from researcher_python.errors import ExportError
from researcher_python.models import Author, Paper


def _core() -> Paper:
    paper = Paper(
        doi="10.1/a",
        year=2020,
        title="AI governance in practice",
        authors=[Author("Smith, Ann"), Author("Jones, Bo")],
        venue="Journal of Governance",
        volume="4",
        issue="2",
        pages="1-20",
        work_type="journal-article",
        abstract="A study, of governance.",
        is_open_access=True,
        citation_count=12,
    )
    return paper.label_as("core").note("Key framework, re-read for the lit review.")


def _general() -> Paper:
    paper = Paper(
        arxiv_id="2101.00001",
        year=2021,
        title="Agents and society",
        venue="A conference",
        work_type="proceedings-article",
        keywords=["agents", "society"],
    )
    return paper.label_as("general")


def _excluded() -> Paper:
    paper = Paper(
        isbn="978-3-031-24863-4",
        year=2019,
        title="A textbook, off topic",
        authors=[Author("Kizza, Joseph Migga")],
        publisher="Springer",
        work_type="book",
    )
    return paper.label_as("excluded", "not about AI governance")


ALL = [_core(), _general(), _excluded()]


class TestTriageState:
    def test_label_defaults_to_unreviewed(self) -> None:
        assert Paper().triage_label() == "unreviewed"

    def test_unknown_label_reads_as_unreviewed(self) -> None:
        assert Paper(label="nonsense").triage_label() == "unreviewed"

    def test_unknown_label_cannot_be_set(self) -> None:
        with pytest.raises(ValueError, match="unknown triage label"):
            Paper().label_as("maybe")

    def test_exclusion_reason_is_kept_only_for_excluded(self) -> None:
        paper = Paper().label_as("excluded", "off topic")
        assert paper.exclusion_reason == "off topic"
        paper.label_as("core")
        assert paper.exclusion_reason == ""

    def test_is_excluded_is_true_only_for_excluded(self) -> None:
        assert _excluded().is_excluded() is True
        assert _core().is_excluded() is False

    def test_note_is_dated_and_appends(self) -> None:
        paper = Paper().note("first").note("second")
        assert len(paper.notes.splitlines()) == 2
        assert paper.notes.endswith("] second")

    def test_triage_state_round_trips_through_to_dict(self) -> None:
        restored = Paper.from_dict(_excluded().to_dict())
        assert restored.label == "excluded"
        assert restored.exclusion_reason == "not about AI governance"

    def test_notes_round_trip(self) -> None:
        restored = Paper.from_dict(_core().to_dict())
        assert "Key framework" in restored.notes


class TestLiteratureMatrix:
    def test_default_columns_include_identity_and_reader_state(self) -> None:
        row = matrix.matrix_row(_core())
        for name in ("key", "label", "authors", "year", "title", "notes"):
            assert name in row
        assert row["label"] == "core"
        assert row["key"] == "10.1/a"

    def test_authors_are_semicolon_joined(self) -> None:
        assert matrix.matrix_row(_core())["authors"] == "Smith, Ann; Jones, Bo"

    def test_row_order_follows_the_input(self) -> None:
        rows = matrix.matrix_rows(ALL, ("year",))
        assert [r["year"] for r in rows] == ["2020", "2021", "2019"]

    def test_unknown_column_is_a_named_error(self) -> None:
        with pytest.raises(ExportError, match="unknown matrix column"):
            matrix.matrix_rows(ALL, ("nope",))

    def test_column_names_lists_every_extractor(self) -> None:
        names = matrix.column_names()
        assert "exclusion_reason" in names and "open_access" in names

    def test_csv_has_a_header_and_one_row_per_paper(self) -> None:
        text = matrix.render_matrix_csv(ALL, ("key", "label"))
        rows = list(csv.reader(io.StringIO(text)))
        assert rows[0] == ["key", "label"]
        assert len(rows) == 4

    def test_csv_quotes_a_value_containing_a_comma(self) -> None:
        text = matrix.render_matrix_csv([_core()], ("authors",))
        assert '"Smith, Ann; Jones, Bo"' in text

    def test_csv_quotes_an_embedded_newline_from_notes(self) -> None:
        text = matrix.render_matrix_csv(ALL, ("key", "notes"))
        # One physical line per record, so the note's newline is quoted.
        assert text.count("\n") == 4

    def test_markdown_has_a_header_and_a_separator(self) -> None:
        lines = matrix.render_matrix_markdown(ALL, ("key", "title")).strip().split("\n")
        assert lines[0] == "| key | title |"
        assert lines[1] == "| --- | --- |"
        assert len(lines) == 5

    def test_markdown_escapes_a_pipe_in_a_cell(self) -> None:
        paper = Paper(doi="10.1/p", title="A | B")
        assert "\\|" in matrix.render_matrix_markdown([paper], ("title",))

    def test_empty_input_still_renders_a_header(self) -> None:
        assert matrix.render_matrix_csv([], ("key",)).strip() == "key"

    def test_writers_accept_a_path_and_a_file_object(self, tmp_path) -> None:
        target = tmp_path / "m.csv"
        matrix.to_matrix_csv(ALL, target, ("key",))
        assert target.exists()
        buffer = io.StringIO()
        matrix.to_matrix_markdown(ALL, buffer, ("key",))
        assert "| key |" in buffer.getvalue()

    def test_writer_failure_raises_export_error(self, tmp_path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        with pytest.raises(ExportError):
            matrix.to_matrix_csv(ALL, blocker / "m.csv")


class TestSynthesisMatrix:
    def test_cross_tabs_a_dimension_against_labels(self) -> None:
        rows = matrix.synthesis_matrix(ALL, "year")
        assert [r["year"] for r in rows] == ["2019", "2020", "2021"]
        assert rows[1]["core"] == "1"

    def test_every_row_carries_a_total(self) -> None:
        for row in matrix.synthesis_matrix(ALL, "year"):
            assert int(row["total"]) == sum(
                int(row[label]) for label in matrix.TRIAGE_LABELS
            )

    def test_label_dimension_is_single_valued(self) -> None:
        rows = matrix.synthesis_matrix(ALL, "label")
        assert {r["label"] for r in rows} == {"core", "general", "excluded"}

    def test_unknown_dimension_is_a_named_error(self) -> None:
        with pytest.raises(ExportError, match="unknown synthesis dimension"):
            matrix.synthesis_matrix(ALL, "colour")

    def test_csv_and_markdown_render(self) -> None:
        assert matrix.render_synthesis_csv(ALL, "year").startswith("year,unreviewed")
        assert "| year |" in matrix.render_synthesis_markdown(ALL, "year")

    def test_writers_round_trip_through_a_path(self, tmp_path) -> None:
        target = tmp_path / "s.csv"
        matrix.to_synthesis_csv(ALL, target, "year")
        assert "total" in target.read_text()
        matrix.to_synthesis_markdown(ALL, io.StringIO(), "year")


class TestScreenSummary:
    def test_counts_every_label_plus_a_total(self) -> None:
        counts = matrix.screen_summary(ALL)
        assert counts["core"] == 1
        assert counts["general"] == 1
        assert counts["excluded"] == 1
        assert counts["unreviewed"] == 0
        assert counts["total"] == 3

    def test_empty_input_reports_zero(self) -> None:
        assert matrix.screen_summary([])["total"] == 0

class TestScreeningReport:
    """The matrix is defined by its triage decisions, so say so out loud."""

    def test_fully_screened_set_is_ready(self) -> None:
        report = matrix.screening_report(ALL)
        assert report["ready"] is True
        assert report["screened"] == 3
        assert report["message"] == ""

    def test_unscreened_set_is_not_ready_and_says_why(self) -> None:
        raw = [_core(), Paper(doi="10.1/new", title="Untriaged hit")]
        report = matrix.screening_report(raw)
        assert report["ready"] is False
        assert report["unreviewed"] == 1
        assert report["screened"] == 1
        assert "1 of 2 records are still 'unreviewed'" in report["message"]
        # The message must be actionable, not just a verdict.
        assert "label" in report["message"]

    def test_empty_set_is_never_ready(self) -> None:
        report = matrix.screening_report([])
        assert report["ready"] is False
        assert report["total"] == 0
        assert "no records" in report["message"]

    def test_report_carries_the_screen_summary_counts(self) -> None:
        report = matrix.screening_report(ALL)
        assert report["core"] == 1 and report["total"] == 3

    def test_excluded_records_count_as_screened(self) -> None:
        # An exclusion is a decision, so an excluded set is still a screened one.
        papers = [_core(), Paper(doi="10.1/x", title="x").label_as("excluded", "off topic")]
        assert matrix.screening_report(papers)["ready"] is True


class TestRequireScreening:
    """``require_screening=True`` refuses to emit an unscreened matrix."""

    def _unscreened(self):
        return [Paper(doi="10.1/a", title="A"), Paper(doi="10.1/b", title="B")]

    def test_csv_refuses_an_unscreened_set(self) -> None:
        with pytest.raises(ExportError, match="screening incomplete"):
            matrix.render_matrix_csv(self._unscreened(), require_screening=True)

    def test_markdown_refuses_an_unscreened_set(self) -> None:
        with pytest.raises(ExportError, match="screening incomplete"):
            matrix.render_matrix_markdown(self._unscreened(), require_screening=True)

    def test_synthesis_refuses_an_unscreened_set(self) -> None:
        with pytest.raises(ExportError, match="screening incomplete"):
            matrix.render_synthesis_csv(self._unscreened(), require_screening=True)
        with pytest.raises(ExportError, match="screening incomplete"):
            matrix.render_synthesis_markdown(self._unscreened(), require_screening=True)

    def test_empty_set_is_refused(self) -> None:
        with pytest.raises(ExportError, match="no records"):
            matrix.render_matrix_csv([], require_screening=True)

    def test_screened_set_renders_normally(self) -> None:
        assert matrix.render_matrix_csv(ALL, require_screening=True)

    def test_gate_is_off_by_default(self) -> None:
        # Backwards compatible: an unscreened set still renders unless asked.
        assert matrix.render_matrix_csv(self._unscreened())

    def test_writers_honour_the_gate(self, tmp_path) -> None:
        target = tmp_path / "gated.csv"
        with pytest.raises(ExportError):
            matrix.to_matrix_csv(self._unscreened(), target, require_screening=True)
        matrix.to_matrix_csv(ALL, target, require_screening=True)
        assert target.read_text().startswith("key,label")


class TestVenueNormalisation:
    """One journal, however the sources spell it, is one row in the cross-tab.

    Sources spell the same venue several ways ("AI and Ethics", "AI and
    ethics", "AI and Ethics (2026), 6, Article 281"). Cross-tabbing the raw
    string fragments one venue across several rows and hides the counts a
    synthesis matrix exists to show.
    """

    def _papers(self):
        return [
            Paper(doi="10.1/a", title="A", venue="AI and Ethics"),
            Paper(doi="10.1/b", title="B", venue="AI and ethics"),
            Paper(doi="10.1/c", title="C", venue="AI and Ethics (2026), 6, Article 281"),
            Paper(doi="10.1/d", title="D", venue="Communications of the ACM"),
        ]

    def test_variants_collapse_into_one_row(self) -> None:
        rows = matrix.synthesis_matrix(self._papers(), "venue")
        venues = {r["venue"]: r for r in rows}
        assert len(venues) == 2
        assert venues["AI and Ethics"]["total"] == "3"

    def test_case_variants_collapse(self) -> None:
        rows = matrix.synthesis_matrix(self._papers(), "venue")
        assert all(r["venue"] == r["venue"].strip() for r in rows)
        assert not any(r["venue"] == "AI and ethics" for r in rows)

    def test_display_keeps_a_readable_spelling(self) -> None:
        rows = matrix.synthesis_matrix(self._papers(), "venue")
        labels = {r["venue"] for r in rows}
        assert "AI and Ethics" in labels
        assert "Communications of the ACM" in labels

    def test_other_dimensions_are_not_affected(self) -> None:
        rows = matrix.synthesis_matrix(self._papers(), "year")
        assert all(r["year"] == "(unknown)" for r in rows)

    def test_counts_are_still_correct_after_merging(self) -> None:
        papers = self._papers()
        papers[0].label_as("core")
        papers[1].label_as("core")
        papers[2].label_as("excluded", "off topic")
        rows = {r["venue"]: r for r in matrix.synthesis_matrix(papers, "venue")}
        assert rows["AI and Ethics"]["core"] == "2"
        assert rows["AI and Ethics"]["excluded"] == "1"
        assert rows["AI and Ethics"]["total"] == "3"


class TestCitationsPerYear:
    """Raw citation counts reward age; the ratio is the standing signal."""

    def _p(self, cites, year):
        return Paper(doi=f"10.1/{cites}-{year}", title="t",
                     citation_count=cites, year=year)

    def test_column_is_available(self) -> None:
        assert "citations_per_year" in matrix.column_names()

    def test_ratio_normalises_for_age(self) -> None:
        old = self._p(400, 1992)
        new = self._p(400, 2023)
        row_old = matrix.matrix_row(old, ("citations_per_year",))
        row_new = matrix.matrix_row(new, ("citations_per_year",))
        assert float(row_new["citations_per_year"]) > float(
            row_old["citations_per_year"]
        )

    def test_unknown_inputs_render_empty_not_zero(self) -> None:
        no_cites = Paper(title="t", year=2020)
        no_year = Paper(title="t", citation_count=10)
        assert matrix.matrix_row(no_cites, ("citations_per_year",))[
            "citations_per_year"
        ] == ""
        assert matrix.matrix_row(no_year, ("citations_per_year",))[
            "citations_per_year"
        ] == ""

    def test_recent_paper_is_not_penalised_for_age(self) -> None:
        # A 2023 paper with 100 citations (25/yr) outranks a 1992 paper with
        # 400 (11.4/yr) — the whole point of normalising. Raw counts would
        # rank the 1992 paper higher purely because it is older.
        assert float(
            matrix.matrix_row(self._p(100, 2023), ("citations_per_year",))[
                "citations_per_year"
            ]
        ) > float(
            matrix.matrix_row(self._p(400, 1992), ("citations_per_year",))[
                "citations_per_year"
            ]
        )
        assert 400 > 100  # ...while the raw counts say the opposite.


class TestTypeNormalisation:
    """'Journal Article' / 'article' / 'journal-article' are one row."""

    def _papers(self):
        return [
            Paper(doi="10.1/a", title="A", work_type="Journal Article"),
            Paper(doi="10.1/b", title="B", work_type="article"),
            Paper(doi="10.1/c", title="C", work_type="journal-article"),
            Paper(doi="10.1/d", title="D", work_type="proceedings-article"),
        ]

    def test_variants_collapse(self) -> None:
        rows = {r["type"]: r for r in matrix.synthesis_matrix(self._papers(), "type")}
        assert len(rows) == 2
        assert rows["article"]["total"] == "3"
        assert rows["proceedings-article"]["total"] == "1"

    def test_display_uses_the_canonical_name(self) -> None:
        rows = {r["type"] for r in matrix.synthesis_matrix(self._papers(), "type")}
        assert "article" in rows
        assert "journal-article" not in rows
        assert "Journal Article" not in rows

    def test_unknown_type_is_its_own_row(self) -> None:
        rows = {r["type"]: r for r in matrix.synthesis_matrix(
            [Paper(doi="10.1/x", title="x")], "type")}
        assert rows["(unknown)"]["total"] == "1"


def test_cite_key_column_is_unique_across_collisions() -> None:
    first = Paper(year=2020, title="Same title", authors=[Author("Smith, Ann")])
    second = Paper(year=2020, title="Same title", authors=[Author("Smith, Ann")])
    rows = matrix.matrix_rows([first, second], ("cite_key",))
    assert [row["cite_key"] for row in rows] == ["smith2020same", "smith2020samea"]


def test_cite_key_can_reuse_a_supplied_map() -> None:
    paper = _core()
    identifier = paper.primary_id()
    rows = matrix.matrix_rows(
        [paper], ("cite_key",), cite_keys={identifier: "custom2020"}
    )
    assert rows[0]["cite_key"] == "custom2020"


def test_cite_key_renders_in_csv_and_markdown() -> None:
    text = matrix.render_matrix_csv([_core()], ("cite_key", "title"))
    assert text.splitlines()[0] == "cite_key,title"
    assert "smith2020" in text.splitlines()[1]
    md = matrix.render_matrix_markdown([_core()], ("cite_key", "title"))
    assert md.splitlines()[0].startswith("| cite_key | title |")
