"""PubMed adapter (NCBI E-utilities over :mod:`Bio.Entrez`).

The flow is deliberately linear — ``esearch`` for a query, ``efetch`` for the
records, ``elink`` for the citation graph — and there is no ``usehistory``/
``WebEnv`` paging because this package never asks a source for more than
``Settings.max_results`` records.

Two things shape the implementation:

* **Scoped XPaths (D6).**  Every path starts at ``./MedlineCitation/…`` or
  ``./PubmedData/…`` and is rooted at the individual ``PubmedArticle``.  An
  unscoped ``.//ArticleId`` walks into ``PubmedData/ReferenceList/Reference``
  and returns the *references'* DOIs alongside the article's own — that bug
  was already found once in this project, so :func:`parse_pubmed_article`
  never uses ``.//`` and ``tests/test_pubmed_parse.py`` pins it.
* **Errors are classified, not swallowed (D8).**  Entrez surfaces failures as
  :class:`urllib.error.HTTPError` (which carries ``.code``) or
  :class:`urllib.error.URLError` (which carries ``.reason``), so
  :func:`map_entrez_error` reads those attributes duck-typed: 429 ->
  ``RateLimitError``, 404 -> ``NotFoundError``, 5xx and everything else ->
  ``SourceError``.

NCBI allows 3 requests/second without an API key and 10 with one, which is
expressed as :attr:`PubMedSource.default_interval`.  NCBI also *requires* a
tool name and an email address, hence ``settings.require("email")`` — a
missing address raises :class:`~researcher_python.errors.ConfigurationError`
before any request is made rather than as a 400 from the API.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from ..config import Settings
from ..errors import NotFoundError, RateLimitError, ResearcherError, SourceError
from ..ids import is_arxiv_id, is_doi, normalize_doi, normalize_orcid, normalize_pmid, wire_doi
from ..models import Author, Paper
from .base import Capabilities, Source, parse_retry_after, require_dependency, squash_text

__all__ = ["PubMedSource", "map_entrez_error", "parse_pubmed_article"]

#: NCBI: 3 requests/s keyless, 10 requests/s with ``RESEARCHER_NCBI_API_KEY``.
_KEYLESS_INTERVAL = 1.0 / 3.0
_KEYED_INTERVAL = 0.1

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Sept": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
_YEAR_RE = re.compile(r"\b(\d{4})\b")


# ---------------------------------------------------------------------- #
# Pure helpers (D4: element in -> value out, no network)
# ---------------------------------------------------------------------- #


def _text(node: Any) -> str:
    """Text of ``node`` including descendants, with markup collapsed away.

    PubMed titles carry inline ``<i>``/``<b>``/``<sup>`` runs, so ``.text``
    alone would truncate at the first tag; ``itertext`` keeps the prose.
    """
    if node is None:
        return ""
    return squash_text("".join(node.itertext()))


def _month(value: str) -> str:
    if not value:
        return ""
    if value in _MONTHS:
        return f"{_MONTHS[value]:02d}"
    if value.isdigit() and 1 <= int(value) <= 12:
        return f"{int(value):02d}"
    return ""


def _pubdate(node: Any) -> tuple[str, int | None]:
    """``PubDate`` -> (ISO date, year), falling back to ``MedlineDate``.

    Legacy records use a free-text span instead of Year/Month/Day, e.g.
    ``"2019 Dec-2020 Jan"``; the first four-digit year in it wins and the
    ISO date is left empty rather than invented.
    """
    if node is None:
        return "", None

    year_text = _text(node.find("./Year"))
    if year_text:
        match = _YEAR_RE.search(year_text)
        if not match:
            return "", None
        year = int(match.group(1))
        iso = f"{year:04d}"
        month = _month(_text(node.find("./Month")))
        if month:
            iso = f"{iso}-{month}"
            day = _text(node.find("./Day"))
            if day.isdigit():
                iso = f"{iso}-{int(day):02d}"
        return iso, year

    medline = _text(node.find("./MedlineDate"))
    if medline:
        match = _YEAR_RE.search(medline)
        if match:
            return "", int(match.group(1))
    return "", None


def _abstract(elem: ET.Element) -> str:
    """Structured abstract, each section prefixed with its ``Label``."""
    parts: list[str] = []
    for node in elem.findall("./MedlineCitation/Article/Abstract/AbstractText"):
        body = _text(node)
        if not body:
            continue
        label = squash_text(node.get("Label"))
        parts.append(f"{label}: {body}" if label else body)
    return " ".join(parts)


def _authors(elem: ET.Element) -> list[Author]:
    """``LastName + ForeName``; a ``CollectiveName``-only entry becomes one Author."""
    authors: list[Author] = []
    for node in elem.findall("./MedlineCitation/Article/AuthorList/Author"):
        last = _text(node.find("./LastName"))
        fore = _text(node.find("./ForeName")) or _text(node.find("./Initials"))
        collective = _text(node.find("./CollectiveName"))
        if last and fore:
            name = f"{last} {fore}"
        elif last:
            name = last
        elif collective:
            name = collective
        else:
            continue
        affiliations = [
            text
            for text in (_text(a) for a in node.findall("./AffiliationInfo/Affiliation"))
            if text
        ]
        authors.append(
            Author(
                name=name,
                orcid=normalize_orcid(_text(node.find("./Identifier[@Source='ORCID']"))),
                affiliations=affiliations,
            )
        )
    return authors


def parse_pubmed_article(elem: ET.Element) -> Paper:
    """Turn one ``PubmedArticle`` element into a :class:`~researcher_python.models.Paper`.

    Pure (D4): the element is already in memory, so fixtures exercise every
    branch without a socket.  Every XPath is scoped from the article root (D6)
    — there is no ``.//`` anywhere in this function.
    """
    pmid = _text(elem.find("./MedlineCitation/PMID"))

    title = _text(elem.find("./MedlineCitation/Article/ArticleTitle"))
    if not title:
        title = _text(elem.find("./MedlineCitation/Article/VernacularTitle"))

    journal = _text(elem.find("./MedlineCitation/Article/Journal/Title")) or _text(
        elem.find("./MedlineCitation/Article/Journal/ISOAbbreviation")
    )
    published, year = _pubdate(
        elem.find("./MedlineCitation/Article/Journal/JournalIssue/PubDate")
    )

    doi = normalize_doi(
        _text(elem.find("./PubmedData/ArticleIdList/ArticleId[@IdType='doi']"))
        or _text(elem.find("./MedlineCitation/Article/ELocationID[@EIdType='doi']"))
    )
    pmcid = _text(elem.find("./PubmedData/ArticleIdList/ArticleId[@IdType='pmc']"))

    publication_types = [
        text
        for text in (
            _text(p) for p in elem.findall("./MedlineCitation/Article/PublicationTypeList/PublicationType")
        )
        if text
    ]
    work_type = publication_types[0] if publication_types else ""
    if not work_type:
        article = elem.find("./MedlineCitation/Article")
        work_type = squash_text(article.get("PubModel")) if article is not None else ""

    categories = [
        text
        for text in (
            _text(d)
            for d in elem.findall("./MedlineCitation/MeshHeadingList/MeshHeading/DescriptorName")
        )
        if text
    ]
    keywords = [
        text
        for text in (_text(k) for k in elem.findall("./MedlineCitation/KeywordList/Keyword"))
        if text
    ]

    paper = Paper(
        sources=["pubmed"],
        ids={"pubmed": pmid} if pmid else {},
        doi=doi,
        pmid=pmid,
        pmcid=pmcid,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
        # capabilities.pdf is False: PubMed gives no open-access PDF URL.
        pdf_url="",
        title=title,
        abstract=_abstract(elem),
        authors=_authors(elem),
        year=year,
        published=published,
        venue=journal,
        volume=_text(elem.find("./MedlineCitation/Article/Journal/JournalIssue/Volume")),
        issue=_text(elem.find("./MedlineCitation/Article/Journal/JournalIssue/Issue")),
        pages=_text(elem.find("./MedlineCitation/Article/Pagination/MedlinePgn")),
        language=_text(elem.find("./MedlineCitation/Article/Language")),
        work_type=work_type,
        categories=categories,
        keywords=keywords,
    )
    if pmid:
        paper.ids.setdefault("pubmed", pmid)
    if doi:
        paper.ids.setdefault("doi", doi)
    if pmcid:
        paper.ids.setdefault("pmc", pmcid)
    return paper


def map_entrez_error(source: str, exc: BaseException) -> ResearcherError:
    """Classify an Entrez/urllib failure onto the package's error hierarchy (D8).

    Duck-typed on ``.code`` / ``.reason`` so tests can pass stand-in
    exceptions; anything without a numeric ``code`` is a transport failure and
    becomes :class:`SourceError` — never :class:`SourceUnavailableError`,
    which is reserved for adapters that cannot be constructed (D5).
    """
    status = getattr(exc, "code", None)
    reason = getattr(exc, "reason", None)
    detail = squash_text(reason) or squash_text(exc)

    if not isinstance(status, int) or isinstance(status, bool):
        return SourceError(source, detail or "PubMed request failed", cause=exc)
    if status == 429:
        return RateLimitError(
            source,
            "NCBI rate limit exceeded; set RESEARCHER_NCBI_API_KEY for 10 req/s",
            retry_after=parse_retry_after(getattr(exc, "headers", None)),
            cause=exc,
        )
    if status == 404:
        return NotFoundError(source, detail or "PubMed has no such record", cause=exc)
    return SourceError(source, detail or f"PubMed returned HTTP {status}", cause=exc)


# ---------------------------------------------------------------------- #
# Adapter
# ---------------------------------------------------------------------- #


class PubMedSource(Source):
    """NCBI PubMed through ``Bio.Entrez``."""

    name = "pubmed"
    capabilities = Capabilities(
        search=True,
        get=True,
        references=True,
        citations=True,
        pdf=False,
    )

    def __init__(self, settings: Settings | None = None, **_: object) -> None:
        super().__init__(settings)
        self._entrez = require_dependency("Bio.Entrez", self.name)
        # NCBI requires tool + email on every request; fail on construction,
        # not as a 400 from the API.
        self._entrez.email = self.settings.require("email")
        self._entrez.tool = "researcher-python"
        if self.settings.ncbi_api_key:
            self._entrez.api_key = self.settings.ncbi_api_key

    @property
    def default_interval(self) -> float:
        return _KEYED_INTERVAL if self.settings.ncbi_api_key else _KEYLESS_INTERVAL

    # -- transport -------------------------------------------------------

    def _call(self, fn: Any, **kwargs: Any) -> Any:
        """Run one Entrez call and return its raw body; classify any failure."""
        self.wait()
        try:
            handle = fn(**kwargs)
            try:
                return handle.read()
            finally:
                handle.close()
        except Exception as exc:  # noqa: BLE001 - classified, then re-raised
            raise map_entrez_error(self.name, exc) from exc

    def _parse(self, data: Any) -> ET.Element:
        if isinstance(data, str):
            data = data.encode("utf-8")
        try:
            return ET.fromstring(data)
        except ET.ParseError as exc:
            raise SourceError(self.name, f"PubMed returned malformed XML: {exc}", cause=exc) from exc
        except (TypeError, ValueError) as exc:
            raise SourceError(self.name, "PubMed returned no data", cause=exc) from exc

    # -- E-utilities -----------------------------------------------------

    def _esearch(self, term: str, retmax: int) -> list[str]:
        root = self._parse(
            self._call(
                self._entrez.esearch,
                db="pubmed",
                term=term,
                retmax=int(retmax),
                retmode="xml",
            )
        )
        # No usehistory/WebEnv: max_results is small enough to page by id.
        return [
            squash_text(node.text)
            for node in root.findall("./IdList/Id")
            if squash_text(node.text)
        ]

    def _efetch(self, pmids: list[str]) -> list[Paper]:
        if not pmids:
            return []
        root = self._parse(
            self._call(
                self._entrez.efetch,
                db="pubmed",
                id=",".join(pmids),
                retmode="xml",
            )
        )
        return [parse_pubmed_article(node) for node in root.findall("./PubmedArticle")]

    def _elink(self, linkname: str, pmid: str) -> list[str]:
        root = self._parse(
            self._call(
                self._entrez.elink,
                dbfrom="pubmed",
                db="pubmed",
                linkname=linkname,
                id=pmid,
                retmode="xml",
            )
        )
        ids: list[str] = []
        for block in root.findall("./LinkSet/LinkSetDb"):
            found = squash_text(block.find("./LinkName"))
            if found and found != linkname:
                continue
            ids.extend(
                squash_text(node.text) for node in block.findall("./Link/Id") if squash_text(node.text)
            )
        return ids

    # -- identity --------------------------------------------------------

    @staticmethod
    def _hint(key: str) -> str:
        if is_arxiv_id(key):
            return f"PubMed looks up PMIDs and DOIs only; use the 'arxiv' source (got {key!r})"
        return f"PubMed looks up PMIDs and DOIs only, got {key!r}"

    def _require_pmid(self, paper: Paper) -> str:
        pmid = normalize_pmid(paper.pmid)
        if pmid:
            return pmid
        if paper.doi:
            found = self._esearch(f"{wire_doi(paper.doi)}[DOI]", 1)
            if found:
                return found[0]
        raise NotFoundError(
            self.name, f"record has no PMID to resolve against PubMed: {paper.title!r}"
        )

    # -- operations ------------------------------------------------------

    def search(self, query: str, *, limit: int | None = None) -> list[Paper]:
        self.require_support("search")
        want = self.settings.max_results if limit is None else max(0, int(limit))
        text = (query or "").strip()
        if want == 0 or not text:
            return []
        return self._efetch(self._esearch(text, want))

    def get(self, key: str) -> Paper:
        self.require_support("get")
        pmid = normalize_pmid(key)
        if pmid:
            papers = self._efetch([pmid])
            if not papers:
                raise NotFoundError(self.name, f"no PubMed record for PMID {pmid!r}")
            return papers[0]
        if is_doi(key):
            # D3: the DOI goes to E-utilities exactly as the caller wrote it.
            doi = wire_doi(key)
            pmids = self._esearch(f"{doi}[DOI]", 1)
            papers = self._efetch(pmids) if pmids else []
            if not papers:
                raise NotFoundError(self.name, f"no PubMed record for DOI {doi!r}")
            return papers[0]
        raise NotFoundError(self.name, self._hint(key))

    def references(self, paper: Paper) -> list[Paper]:
        self.require_support("references")
        return self._linked(paper, "pubmed_pubmed_refs")

    def citations(self, paper: Paper) -> list[Paper]:
        self.require_support("citations")
        return self._linked(paper, "pubmed_pubmed_citedin")

    def _linked(self, paper: Paper, linkname: str) -> list[Paper]:
        pmid = self._require_pmid(paper)
        linked = self._elink(linkname, pmid)[: max(0, int(self.settings.max_results))]
        return self._efetch(linked)
