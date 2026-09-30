"""Offline tests for :mod:`researcher_python.download` (tmp_path + fake responses)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import requests

import researcher_python.download as download
from researcher_python.config import Settings
from researcher_python.download import DownloadResult, download_pdf, parse_unpaywall
from researcher_python.errors import (
    ConfigurationError,
    DownloadError,
    NoPdfError,
    RateLimitError,
    SourceError,
)
from researcher_python.models import Paper

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\n%%EOF\n"
HTML_BYTES = b"<html><body>Not a PDF</body></html>"


class FakeResponse:
    """Minimal stand-in for a requests.Response."""

    def __init__(
        self,
        status_code: int = 200,
        *,
        chunks: list[bytes] | None = None,
        text: str = "",
        json_payload: Any = None,
    ) -> None:
        self.status_code = status_code
        self._chunks = chunks if chunks is not None else []
        self.text = text
        self._json_payload = json_payload
        self.closed = False

    def iter_content(self, chunk_size: int | None = None):  # noqa: ANN201
        yield from self._chunks

    def json(self) -> Any:
        if self._json_payload is None:
            raise ValueError("no json body")
        return self._json_payload

    def close(self) -> None:
        self.closed = True


def pdf_response(*parts: bytes) -> FakeResponse:
    return FakeResponse(200, chunks=list(parts))


def json_response(payload: Any) -> FakeResponse:
    return FakeResponse(200, json_payload=payload)


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch):
    """Patch the transport with an empty router the test fills in."""
    state: dict[str, Any] = {"routes": {}}

    def fake_get(url: str, **kwargs: Any) -> Any:
        state["calls"].append({"url": url, **kwargs})
        for key, value in state["routes"].items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected request: {url}")

    state["calls"] = []
    monkeypatch.setattr(download, "_http_get", fake_get)
    return state


# -- parse_unpaywall -------------------------------------------------------


def test_parse_unpaywall_orders_best_first() -> None:
    urls = parse_unpaywall(
        {
            "best_oa_location": {"url_for_pdf": "https://a.org/best.pdf", "url": "https://a.org/best"},
            "oa_locations": [
                {"url_for_pdf": "https://b.org/other.pdf", "url": "https://b.org/other"},
            ],
        }
    )
    assert urls == [
        "https://a.org/best.pdf",
        "https://a.org/best",
        "https://b.org/other.pdf",
        "https://b.org/other",
    ]


def test_parse_unpaywall_deduplicates() -> None:
    urls = parse_unpaywall(
        {
            "best_oa_location": {"url_for_pdf": "https://a.org/x.pdf"},
            "oa_locations": [
                {"url_for_pdf": "https://a.org/x.pdf"},
                {"url": "https://a.org/x.pdf"},
            ],
        }
    )
    assert urls == ["https://a.org/x.pdf"]


def test_parse_unpaywall_skips_nulls_and_missing_locations() -> None:
    assert parse_unpaywall({"best_oa_location": None, "oa_locations": None}) == []
    assert (
        parse_unpaywall(
            {"best_oa_location": {"url_for_pdf": None, "url": None}}
        )
        == []
    )
    assert parse_unpaywall({"best_oa_location": "not a mapping"}) == []
    assert parse_unpaywall({"not_unpaywall": True}) == []
    assert parse_unpaywall("not a dict") == []  # type: ignore[arg-type]


# -- candidate paths -------------------------------------------------------


def test_first_candidate_is_the_papers_own_pdf_url(http: dict, tmp_path: Path) -> None:
    http["routes"]["example.org/direct"] = pdf_response(PDF_BYTES)
    paper = Paper(title="Direct", pdf_url="https://example.org/direct.pdf", doi="10.1/x")

    result = download_pdf(paper, tmp_path)

    assert isinstance(result, DownloadResult)
    assert result.via == "paper"
    assert result.url == "https://example.org/direct.pdf"
    assert result.path.read_bytes() == PDF_BYTES
    # No Unpaywall call, because step 1 already had a URL.
    assert all("unpaywall" not in c["url"] for c in http["calls"])


def test_second_candidate_falls_back_to_unpaywall(
    http: dict, tmp_path: Path
) -> None:
    http["routes"]["unpaywall.org"] = json_response(
        {"best_oa_location": {"url_for_pdf": "https://example.org/upw.pdf"}}
    )
    http["routes"]["example.org/upw"] = pdf_response(PDF_BYTES)
    paper = Paper(title="Fallback", doi="10.1234/upw")

    result = download_pdf(paper, tmp_path, settings=Settings(email="a@example.com"))

    assert result.via == "unpaywall"
    assert result.url == "https://example.org/upw.pdf"
    unpaywall_calls = [c for c in http["calls"] if "unpaywall.org" in c["url"]]
    assert len(unpaywall_calls) == 1
    assert "10.1234/upw" in unpaywall_calls[0]["url"]
    assert "email=a%40example.com" in unpaywall_calls[0]["url"]


def test_paper_url_wins_over_unpaywall(http: dict, tmp_path: Path) -> None:
    http["routes"]["example.org/direct"] = pdf_response(PDF_BYTES)
    paper = Paper(
        title="Ordered",
        pdf_url="https://example.org/direct.pdf",
        doi="10.1/x",
    )

    result = download_pdf(paper, tmp_path, settings=Settings(email="a@example.com"))

    assert result.via == "paper"
    assert all("unpaywall" not in c["url"] for c in http["calls"])


def test_falls_through_when_the_first_candidate_returns_html(
    http: dict, tmp_path: Path
) -> None:
    http["routes"]["example.org/landing"] = pdf_response(HTML_BYTES)
    http["routes"]["unpaywall.org"] = json_response(
        {"best_oa_location": {"url_for_pdf": "https://example.org/real.pdf"}}
    )
    http["routes"]["example.org/real"] = pdf_response(PDF_BYTES)
    paper = Paper(
        title="Landing page",
        pdf_url="https://example.org/landing.pdf",
        doi="10.1/x",
    )

    result = download_pdf(paper, tmp_path, settings=Settings(email="a@example.com"))

    assert result.via == "unpaywall"
    assert result.url == "https://example.org/real.pdf"
    assert result.path.read_bytes() == PDF_BYTES


# -- failures --------------------------------------------------------------


def test_no_candidates_at_all_raises_no_pdf_error(
    http: dict, tmp_path: Path
) -> None:
    paper = Paper(title="Nothing known")

    with pytest.raises(NoPdfError) as excinfo:
        download_pdf(paper, tmp_path)

    assert "Nothing known" in str(excinfo.value)
    assert http["calls"] == []  # no network at all


def test_unpaywall_404_is_no_candidates_not_an_error(http: dict, tmp_path: Path) -> None:
    http["routes"]["unpaywall.org"] = FakeResponse(404, text="Not found")
    paper = Paper(title="Unknown doi", doi="10.1/absent")

    with pytest.raises(NoPdfError):
        download_pdf(paper, tmp_path, settings=Settings(email="a@example.com"))


def test_unpaywall_429_is_a_rate_limit_error(http: dict, tmp_path: Path) -> None:
    http["routes"]["unpaywall.org"] = FakeResponse(429, text="slow down")
    paper = Paper(title="Throttled", doi="10.1/x")

    with pytest.raises(RateLimitError) as excinfo:
        download_pdf(paper, tmp_path, settings=Settings(email="a@example.com"))
    assert excinfo.value.source == "unpaywall"


def test_unpaywall_5xx_is_a_source_error(http: dict, tmp_path: Path) -> None:
    http["routes"]["unpaywall.org"] = FakeResponse(503, text="upstream broke")
    paper = Paper(title="Broken", doi="10.1/x")

    with pytest.raises(SourceError) as excinfo:
        download_pdf(paper, tmp_path, settings=Settings(email="a@example.com"))
    assert excinfo.value.source == "unpaywall"
    assert "upstream broke" in str(excinfo.value)


def test_unpaywall_network_error_is_a_source_error(http: dict, tmp_path: Path) -> None:
    http["routes"]["unpaywall.org"] = requests.ConnectionError("refused")
    paper = Paper(title="Offline", doi="10.1/x")

    with pytest.raises(SourceError) as excinfo:
        download_pdf(paper, tmp_path, settings=Settings(email="a@example.com"))
    assert isinstance(excinfo.value.__cause__, requests.ConnectionError)


def test_missing_email_is_raised_only_when_step_three_is_reached(
    http: dict, tmp_path: Path
) -> None:
    # Step 3 reached: DOI, no other candidate, no email.
    with pytest.raises(ConfigurationError) as excinfo:
        download_pdf(Paper(title="Needs email", doi="10.1/x"), tmp_path)
    assert "RESEARCHER_EMAIL" in str(excinfo.value)
    assert http["calls"] == []  # failed before any request

    # Step 3 never reached: the paper already carries a PDF URL.
    http["routes"]["example.org/direct"] = pdf_response(PDF_BYTES)
    result = download_pdf(
        Paper(title="Has url", doi="10.1/x", pdf_url="https://example.org/direct.pdf"),
        tmp_path,
    )
    assert result.via == "paper"

    # Step 3 never reached: no DOI, so Unpaywall is not consulted at all.
    with pytest.raises(NoPdfError):
        download_pdf(Paper(title="No doi"), tmp_path)


def test_all_candidates_failing_aggregates_each_reason(
    http: dict, tmp_path: Path
) -> None:
    http["routes"]["example.org/landing"] = pdf_response(HTML_BYTES)
    http["routes"]["unpaywall.org"] = json_response(
        {"best_oa_location": {"url_for_pdf": "https://example.org/also-html.pdf"}}
    )
    http["routes"]["example.org/also-html"] = pdf_response(HTML_BYTES)
    paper = Paper(
        title="Everything fails", pdf_url="https://example.org/landing.pdf", doi="10.1/x"
    )

    with pytest.raises(DownloadError) as excinfo:
        download_pdf(paper, tmp_path, settings=Settings(email="a@example.com"))

    message = str(excinfo.value)
    assert "paper: https://example.org/landing.pdf" in message
    assert "unpaywall: https://example.org/also-html.pdf" in message
    assert "was not a PDF" in message
    assert "\n  - " in message  # one candidate per line


def test_part_file_is_removed_after_a_failed_download(
    http: dict, tmp_path: Path
) -> None:
    http["routes"]["example.org/direct"] = pdf_response(HTML_BYTES)
    paper = Paper(title="Html", pdf_url="https://example.org/direct.pdf")

    with pytest.raises(DownloadError):
        download_pdf(paper, tmp_path)

    assert list(tmp_path.glob("*.part")) == []
    assert list(tmp_path.glob("*.pdf")) == []


def test_part_file_is_removed_when_the_transfer_errors(
    http: dict, tmp_path: Path
) -> None:
    http["routes"]["example.org/direct"] = requests.ConnectionError("reset")
    paper = Paper(title="Reset", pdf_url="https://example.org/direct.pdf")

    with pytest.raises(DownloadError) as excinfo:
        download_pdf(paper, tmp_path)

    assert "reset" in str(excinfo.value)
    assert list(tmp_path.glob("*.part")) == []


# -- existing files --------------------------------------------------------


def test_existing_file_is_returned_without_any_network_call(
    http: dict, tmp_path: Path
) -> None:
    paper = Paper(title="Already here", doi="10.1/x")
    target = tmp_path / download._filename(paper)
    target.write_bytes(b"%PDF-1.4\nexisting\n")

    result = download_pdf(paper, tmp_path)

    assert result.via == "existing"
    assert result.path == target
    assert result.path.read_bytes() == b"%PDF-1.4\nexisting\n"
    assert http["calls"] == []


def test_overwrite_replaces_the_existing_file(http: dict, tmp_path: Path) -> None:
    http["routes"]["example.org/direct"] = pdf_response(PDF_BYTES)
    paper = Paper(title="Over me", pdf_url="https://example.org/direct.pdf")
    target = tmp_path / download._filename(paper)
    target.write_bytes(b"old contents")

    result = download_pdf(paper, tmp_path, overwrite=True)

    assert result.via == "paper"
    assert result.path.read_bytes() == PDF_BYTES
    assert list(tmp_path.glob("*.part")) == []


# -- naming ----------------------------------------------------------------


def test_filename_slug_and_id_are_stable_and_ascii(tmp_path: Path) -> None:
    paper = Paper(title="Étude über Grüße: 音楽と “時間”", doi="10.1103/PhysRevLett.x")
    name = download._filename(paper)

    assert name == download._filename(paper)  # stable across calls
    assert name.endswith(".pdf")
    stem = name[: -len(".pdf")]
    assert stem.isascii()
    assert re.fullmatch(r"[a-z0-9-]+-[0-9a-f]{8}", stem), name
    # The id is the first 8 chars of sha1(DOI).
    import hashlib

    assert stem.endswith(hashlib.sha1(b"10.1103/PhysRevLett.x").hexdigest()[:8])


def test_filename_id_falls_back_through_the_identifiers() -> None:
    import hashlib

    arxiv_only = Paper(title="T", arxiv_id="1706.03762")
    assert download._filename(arxiv_only).endswith(
        "-" + hashlib.sha1(b"1706.03762").hexdigest()[:8] + ".pdf"
    )

    title_only = Paper(title="No identifiers at all")
    assert download._filename(title_only).endswith(
        "-" + hashlib.sha1(b"No identifiers at all").hexdigest()[:8] + ".pdf"
    )


def test_empty_title_slug_falls_back_to_untitled() -> None:
    assert download._slug("") == "untitled"
    assert download._slug("音楽") == "untitled"


def test_output_dir_is_created_when_missing(http: dict, tmp_path: Path) -> None:
    http["routes"]["example.org/direct"] = pdf_response(PDF_BYTES)
    nested = tmp_path / "deep" / "nested"

    result = download_pdf(
        Paper(title="Nested", pdf_url="https://example.org/direct.pdf"), nested
    )

    assert result.path.parent == nested
    assert nested.is_dir()
    assert result.path.exists()
