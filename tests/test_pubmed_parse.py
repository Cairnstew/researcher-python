"""Offline tests for the PubMed adapter (D1: no network, D4: pure parsing, D6: scoped XPaths)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from researcher_python.errors import (
    ConfigurationError,
    NotFoundError,
    RateLimitError,
    SourceError,
    SourceUnavailableError,
)
from researcher_python.config import Settings
from researcher_python.models import Paper
from researcher_python.sources.pubmed_source import (
    PubMedSource,
    map_entrez_error,
    parse_pubmed_article,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "pubmed_article.xml"


def load_root() -> ET.Element:
    return ET.fromstring(_FIXTURE.read_bytes())


def articles() -> list[ET.Element]:
    return load_root().findall("./PubmedArticle")


# -- parsing ---------------------------------------------------------------


def test_fixture_has_the_four_expected_articles() -> None:
    assert len(articles()) == 4


def test_parses_structured_article() -> None:
    paper = parse_pubmed_article(articles()[0])

    assert paper.sources == ["pubmed"]
    assert paper.pmid == "30012345"
    # ArticleIdList wins over the ELocationID fallback; lowercased as a key.
    assert paper.doi == "10.1234/struct.2021"
    assert paper.pmcid == "PMC7001234"
    assert paper.ids == {
        "pubmed": "30012345",
        "doi": "10.1234/struct.2021",
        "pmc": "PMC7001234",
    }
    assert paper.url == "https://pubmed.ncbi.nlm.nih.gov/30012345/"
    # Inline <i>/<sup> markup is flattened into prose.
    assert paper.title == "Structured abstract with markup inside"
    assert paper.venue == "Journal of Testing Things"
    assert paper.year == 2021
    assert paper.published == "2021-04-15"
    assert paper.volume == "12"
    assert paper.issue == "4"
    assert paper.pages == "101-112"
    assert paper.language == "eng"
    assert paper.work_type == "Journal Article"
    assert paper.categories == ["Test Methods"]
    assert paper.keywords == ["testing", "fixtures"]
    assert paper.pdf_url == ""
    assert [a.name for a in paper.authors] == ["Smith John A", "García María"]
    assert paper.authors[1].orcid == "0000-0002-1825-0097"
    assert paper.authors[0].affiliations == [
        "Department of Testing, Example University."
    ]


def test_structured_abstract_joins_sections_with_labels() -> None:
    abstract = parse_pubmed_article(articles()[0]).abstract
    assert abstract == (
        "BACKGROUND: Background text goes here. "
        "METHODS: Methods text goes here. "
        "Results without a label."
    )


def test_medline_date_falls_back_to_the_first_year() -> None:
    paper = parse_pubmed_article(articles()[1])
    assert paper.year == 2019
    # No ISO date can be inferred from a span, so none is invented.
    assert paper.published == ""
    assert paper.doi == "10.5555/legacy.2019"
    assert paper.venue == "Quarterly Annals of Legacy Records"
    assert paper.pmid == "30012346"


def test_no_abstract_and_collective_name_author() -> None:
    paper = parse_pubmed_article(articles()[2])
    assert paper.abstract == ""
    assert [a.name for a in paper.authors] == ["Working Group on Names"]
    assert paper.doi == ""
    assert paper.year == 2018
    assert paper.published == "2018-11"
    # No PublicationTypeList, so PubModel stands in.
    assert paper.work_type == "Electronic"


def test_elocationid_is_used_when_articleidlist_has_no_doi() -> None:
    xml = """
    <PubmedArticle>
      <MedlineCitation>
        <PMID Version="1">42</PMID>
        <Article PubModel="Print">
          <ArticleTitle>T</ArticleTitle>
          <ELocationID EIdType="doi" ValidYN="Y">10.1/Fallback</ELocationID>
        </Article>
      </MedlineCitation>
      <PubmedData>
        <ArticleIdList><ArticleId IdType="pubmed">42</ArticleId></ArticleIdList>
      </PubmedData>
    </PubmedArticle>"""
    paper = parse_pubmed_article(ET.fromstring(xml))
    assert paper.doi == "10.1/fallback"


# -- D6: XPath scoping -----------------------------------------------------


def test_d6_nested_decoy_nodes_do_not_leak_in() -> None:
    """A `.//ArticleId` would return the *reference's* DOI; ours must not."""
    decoy = articles()[3]

    # The decoys really are present in the fixture...
    assert len(decoy.findall(".//ArticleTitle")) > 1
    assert len(decoy.findall(".//ArticleId[@IdType='doi']")) == 1
    assert decoy.findall("./PubmedData/ArticleIdList/ArticleId[@IdType='doi']") == []

    # ...and the parser ignores them.
    paper = parse_pubmed_article(decoy)
    assert paper.title == "The correct top-level title"
    assert "decoy" not in paper.title.lower()
    assert paper.doi == ""
    assert paper.ids == {"pubmed": "30012348"}


def test_d6_unscoped_search_over_the_set_is_polluted() -> None:
    """Demonstrates why every path in the adapter is rooted and prefixed."""
    root = load_root()
    own = root.findall("./PubmedArticle/PubmedData/ArticleIdList/ArticleId[@IdType='doi']")
    everything = root.findall(".//ArticleId[@IdType='doi']")
    assert len(everything) > len(own)  # the nested decoy is the extra one


# -- error classification (D8) ---------------------------------------------


class FakeHttpError(Exception):
    """Stands in for urllib.error.HTTPError, which carries .code/.reason."""

    def __init__(
        self, code: int, reason: str = "", headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(reason or f"HTTP {code}")
        self.code = code
        self.reason = reason
        # urllib.error.HTTPError exposes .headers; parse_retry_after reads it.
        self.headers = headers or {}


class FakeUrlError(Exception):
    """Stands in for urllib.error.URLError, which carries .reason only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def test_429_maps_to_rate_limit_naming_the_api_key() -> None:
    error = map_entrez_error("pubmed", FakeHttpError(429, "Too Many Requests"))
    assert isinstance(error, RateLimitError)
    assert "RESEARCHER_NCBI_API_KEY" in str(error)
    assert error.source == "pubmed"


def test_429_passes_the_servers_retry_after_on() -> None:
    error = map_entrez_error(
        "pubmed", FakeHttpError(429, "Too Many Requests", headers={"Retry-After": "12"})
    )
    assert isinstance(error, RateLimitError)
    assert error.retry_after == 12.0


def test_429_with_unparsable_or_absent_retry_after_maps_to_none() -> None:
    assert map_entrez_error("pubmed", FakeHttpError(429)).retry_after is None
    assert (
        map_entrez_error(
            "pubmed",
            FakeHttpError(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
        ).retry_after
        is None
    )


def test_404_maps_to_not_found() -> None:
    error = map_entrez_error("pubmed", FakeHttpError(404, "Not Found"))
    assert isinstance(error, NotFoundError)
    assert "Not Found" in str(error)


@pytest.mark.parametrize("code", [400, 500, 502, 503])
def test_http_errors_are_source_errors_never_unavailable(code: int) -> None:
    """D8: 5xx -> SourceError; SourceUnavailableError is for adapters only (D5)."""
    error = map_entrez_error("pubmed", FakeHttpError(code))
    assert type(error) is SourceError
    assert not isinstance(error, SourceUnavailableError)


def test_url_error_maps_to_source_error() -> None:
    error = map_entrez_error("pubmed", FakeUrlError("Name or service not known"))
    assert type(error) is SourceError
    assert "Name or service not known" in str(error)


def test_unrecognised_exception_maps_to_source_error() -> None:
    error = map_entrez_error("pubmed", RuntimeError("boom"))
    assert type(error) is SourceError
    assert "boom" in str(error)


# -- adapter behaviour (offline) -------------------------------------------


def test_capability_table() -> None:
    caps = PubMedSource.capabilities
    assert (caps.search, caps.get, caps.references, caps.citations, caps.pdf) == (
        True,
        True,
        True,
        True,
        False,
    )


def test_email_is_required_at_construction() -> None:
    """NCBI needs tool+email; fail before any request rather than as a 400."""
    with pytest.raises(ConfigurationError) as excinfo:
        PubMedSource(Settings())
    assert "RESEARCHER_EMAIL" in str(excinfo.value)


def test_default_interval_reflects_api_key() -> None:
    keyless = PubMedSource(Settings(email="a@example.com"))
    keyed = PubMedSource(Settings(email="a@example.com", ncbi_api_key="k123"))
    assert keyless.default_interval == pytest.approx(1 / 3)  # 3 req/s
    assert keyed.default_interval == pytest.approx(0.1)  # 10 req/s


@pytest.mark.parametrize(
    ("key", "hint"),
    [
        ("arXiv:1706.03762", "use the 'arxiv' source"),
        ("W2741809807", "looks up PMIDs and DOIs only"),
        ("not-an-id", "looks up PMIDs and DOIs only"),
    ],
)
def test_get_rejects_unusable_identifiers_with_a_hint(key: str, hint: str) -> None:
    """Raises before any request: unusable keys never reach E-utilities."""
    with pytest.raises(NotFoundError) as excinfo:
        PubMedSource(Settings(email="a@example.com")).get(key)
    assert hint in str(excinfo.value)


def test_references_without_an_addressable_id_refuses_before_fetching() -> None:
    with pytest.raises(NotFoundError) as excinfo:
        PubMedSource(Settings(email="a@example.com")).references(Paper(title="orphan"))
    assert "no PMID" in str(excinfo.value)
