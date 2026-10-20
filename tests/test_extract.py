"""PDF text and quote extraction — the pure helpers offline, PDF I/O stubbed."""

from __future__ import annotations

import pytest

from researcher_python import extract
from researcher_python.errors import ExportError


class TestPageSpec:
    def test_none_means_every_page(self) -> None:
        assert extract.parse_page_spec(None, 3) == [1, 2, 3]

    def test_blank_means_every_page(self) -> None:
        assert extract.parse_page_spec("   ", 2) == [1, 2]

    def test_a_range_expands(self) -> None:
        assert extract.parse_page_spec("1-3", 9) == [1, 2, 3]

    def test_ranges_and_singles_combine(self) -> None:
        assert extract.parse_page_spec("1-3,7,9", 9) == [1, 2, 3, 7, 9]

    def test_pages_are_sorted_and_deduplicated(self) -> None:
        assert extract.parse_page_spec("5,1-2,5", 9) == [1, 2, 5]

    def test_a_reversed_range_is_normalised(self) -> None:
        assert extract.parse_page_spec("3-1", 9) == [1, 2, 3]

    def test_pages_past_the_end_are_dropped(self) -> None:
        assert extract.parse_page_spec("1-5,7", 3) == [1, 2, 3]

    def test_page_zero_is_ignored(self) -> None:
        assert extract.parse_page_spec("0-2", 5) == [1, 2]

    def test_whitespace_around_the_dash_is_tolerated(self) -> None:
        assert extract.parse_page_spec(" 2 - 4 ", 9) == [2, 3, 4]

    def test_garbage_is_a_named_error(self) -> None:
        with pytest.raises(ExportError, match="cannot read page selection"):
            extract.parse_page_spec("intro", 3)

    def test_zero_pages_yields_nothing(self) -> None:
        assert extract.parse_page_spec(None, 0) == []


class TestQuoteSnippets:
    TEXT = (
        "AI agents raise governance questions. Governance of agents remains "
        "contested. An unrelated sentence closes the page."
    )

    def test_finds_every_occurrence(self) -> None:
        distant = "governance " + ("filler " * 40) + "governance"
        assert len(extract.quote_snippets(distant, "governance")) == 2

    def test_close_hits_merge_into_one_snippet(self) -> None:
        # With the default window the two spans overlap and must not be
        # reported as two near-duplicate quotes.
        assert len(extract.quote_snippets(self.TEXT, "governance")) == 1

    def test_a_snippet_carries_context(self) -> None:
        snippet = extract.quote_snippets(self.TEXT, "unrelated", window=40)[0]
        assert "unrelated" in snippet
        assert len(snippet) > len("unrelated")

    def test_case_insensitive(self) -> None:
        assert extract.quote_snippets(self.TEXT, "GOVERNANCE")

    def test_no_match_yields_nothing(self) -> None:
        assert extract.quote_snippets(self.TEXT, "quantum") == []

    def test_no_terms_returns_the_opening(self) -> None:
        assert extract.quote_snippets(self.TEXT, window=20)[0].startswith("AI agents")

    def test_several_terms_are_all_sought(self) -> None:
        found = extract.quote_snippets(self.TEXT, "agents,quantum")
        assert len(found) == 1  # only the `agents` hit

    def test_empty_text_yields_nothing(self) -> None:
        assert extract.quote_snippets("", "anything") == []
        assert extract.quote_snippets(None, "anything") == []

    def test_overlapping_hits_merge_into_one_snippet(self) -> None:
        text = "alpha beta gamma delta epsilon"
        merged = extract.quote_snippets(text, "alpha,gamma", window=20)
        assert len(merged) == 1
        assert "beta" in merged[0]

    def test_terms_may_be_a_list(self) -> None:
        assert extract.quote_snippets(self.TEXT, ["agents", "quantum"])


# ---------------------------------------------------------------------- #
# PDF I/O, with the reader stubbed so no wheel or file is needed
# ---------------------------------------------------------------------- #


class _FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakeReader:
    def __init__(self, pages: list[str]) -> None:
        self.pages = [_FakePage(t) for t in pages]


@pytest.fixture
def stub_reader(monkeypatch):
    """Install a fake reader over a named set of pages."""
    state: dict[str, list[str]] = {}

    def install(pages: list[str]) -> None:
        state["pages"] = pages
        monkeypatch.setattr(
            extract, "_reader", lambda path: _FakeReader(state["pages"])
        )

    install.pages = state  # type: ignore[attr-defined]
    return install


class TestExtractText:
    def test_whole_file(self, stub_reader) -> None:
        stub_reader(["first page", "second page"])
        text = extract.extract_text("fake.pdf")
        assert text == "first page\n\nsecond page"

    def test_page_range(self, stub_reader) -> None:
        stub_reader(["one", "two", "three"])
        assert extract.extract_text("fake.pdf", pages="2") == "two"
        assert extract.extract_text("fake.pdf", pages="1-2") == "one\n\ntwo"

    def test_pages_are_one_indexed(self, stub_reader) -> None:
        stub_reader(["one", "two"])
        assert extract.extract_text("fake.pdf", pages="1") == "one"


class TestIterPages:
    def test_returns_1_indexed_pairs(self, stub_reader) -> None:
        stub_reader(["a", "b"])
        assert extract.iter_pages("fake.pdf") == [(1, "a"), (2, "b")]

    def test_whitespace_is_collapsed(self, stub_reader) -> None:
        stub_reader(["lots   of\n\nspace"])
        assert extract.iter_pages("fake.pdf")[0][1] == "lots of space"


class TestFindQuotes:
    PAGES = [
        "The governance of AI agents is contested.",
        "Nothing relevant on this page at all.",
        "Agents again, and a final remark about agents.",
    ]

    def test_reports_the_page_each_quote_came_from(self, stub_reader) -> None:
        stub_reader(self.PAGES)
        quotes = extract.find_quotes("fake.pdf", terms="governance")
        assert quotes[0]["page"] == 1
        assert "governance" in quotes[0]["text"]

    def test_finds_across_pages_in_order(self, stub_reader) -> None:
        stub_reader(self.PAGES)
        pages = [q["page"] for q in extract.find_quotes("fake.pdf", terms="agents")]
        assert pages == [1, 3]

    def test_records_the_matched_term(self, stub_reader) -> None:
        stub_reader(self.PAGES)
        assert extract.find_quotes("fake.pdf", terms="governance")[0]["term"] == "governance"

    def test_limit_caps_the_total(self, stub_reader) -> None:
        stub_reader(self.PAGES * 5)
        assert len(extract.find_quotes("fake.pdf", terms="agents", limit=3)) == 3

    def test_page_selection_is_honoured(self, stub_reader) -> None:
        stub_reader(self.PAGES)
        quotes = extract.find_quotes("fake.pdf", terms="agents", pages="1")
        assert [q["page"] for q in quotes] == [1]

    def test_no_terms_returns_the_opening_of_each_page(self, stub_reader) -> None:
        stub_reader(["opening one", "opening two"])
        quotes = extract.find_quotes("fake.pdf", limit=2)
        assert [q["text"] for q in quotes] == ["opening one", "opening two"]

    def test_no_match_returns_nothing(self, stub_reader) -> None:
        stub_reader(self.PAGES)
        assert extract.find_quotes("fake.pdf", terms="quantum") == []

    def test_empty_pages_are_skipped(self, stub_reader) -> None:
        stub_reader(["", "agents here", ""])
        assert [q["page"] for q in extract.find_quotes("fake.pdf", terms="agents")] == [2]


def _install_pypdf(monkeypatch, module) -> None:
    """Put a stand-in ``pypdf`` in sys.modules so no wheel is needed."""
    import sys

    monkeypatch.setitem(sys.modules, "pypdf", module)


class TestMissingDependency:
    def test_a_missing_wheel_names_itself(self, monkeypatch) -> None:
        import sys

        from researcher_python.errors import SourceUnavailableError

        # ``None`` in sys.modules makes importlib raise ImportError.
        _install_pypdf(monkeypatch, None)
        with pytest.raises(SourceUnavailableError, match="pypdf"):
            extract.iter_pages("anything.pdf")
        assert "pypdf" not in sys.modules or sys.modules["pypdf"] is None

    def test_an_unreadable_file_is_an_export_error(self, monkeypatch) -> None:
        def boom(path):
            raise ValueError("not a PDF")

        _install_pypdf(monkeypatch, type("m", (), {"PdfReader": boom}))
        with pytest.raises(ExportError, match="not a readable PDF"):
            extract.iter_pages("broken.pdf")

    def test_an_oserror_from_the_open_is_an_export_error(self, monkeypatch) -> None:
        def missing(path):
            raise OSError("no such file")

        _install_pypdf(monkeypatch, type("m", (), {"PdfReader": missing}))
        with pytest.raises(ExportError, match="cannot read"):
            extract.iter_pages("nope.pdf")

    def test_one_bad_page_does_not_lose_the_file(self, monkeypatch) -> None:
        class BadPage:
            def extract_text(self):
                raise RuntimeError("encrypted")

        class MixedReader:
            pages = [_FakePage("good text"), BadPage()]

        _install_pypdf(monkeypatch, type("m", (), {"PdfReader": lambda p: MixedReader()}))
        assert extract.iter_pages("mixed.pdf") == [(1, "good text"), (2, "")]