"""Identifier normalisation.

Records reach this package from five different APIs, each with its own idea
of what an identifier looks like.  De-duplication only works if
``https://doi.org/10.1000/XYZ``, ``doi:10.1000/xyz`` and ``10.1000/xyz.``
collapse to the same string, so every adapter funnels its raw ids through
the normalisers below before they reach :class:`~researcher_python.models.Paper`.
"""

from __future__ import annotations

import re

_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi.org/",
    "doi:",
)

_ARXIV_PREFIXES = (
    "https://arxiv.org/abs/",
    "http://arxiv.org/abs/",
    "https://arxiv.org/pdf/",
    "http://arxiv.org/pdf/",
    "arxiv.org/abs/",
    "arxiv:",
    "arXiv:",
)

# Legacy (pre-2007) ids look like ``math.GT/0309136``; current ones are
# ``NNNN.NNNNN`` with an optional trailing version.
_LEGACY_ARXIV = re.compile(r"^[a-z-]+(\.[A-Z]{2})?/\d{7}$", re.IGNORECASE)
_CURRENT_ARXIV = re.compile(r"^\d{4}\.\d{4,5}$")
_ARXIV_VERSION = re.compile(r"v\d+$")


def _strip_prefixes(value: str, prefixes: tuple[str, ...]) -> str:
    text = value.strip()
    lowered = text.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix.lower()):
            return text[len(prefix):].strip()
    return text


def normalize_doi(value: str | None) -> str:
    """Return a bare, lowercase DOI, or ``""`` if ``value`` is not a DOI.

    Strips resolver URLs and the ``doi:`` prefix, lowercases (DOI names are
    case-insensitive), and trims trailing punctuation left behind by copying
    a citation out of a PDF.
    """
    if not value:
        return ""
    doi = _strip_prefixes(str(value), _DOI_PREFIXES)
    # Drop a trailing ``.``/``;``/``)`` only when it is clearly citation
    # punctuation rather than part of the suffix.
    doi = doi.strip().rstrip(".,;")
    if not doi.startswith("10."):
        return ""
    return doi.lower()


def is_doi(value: str | None) -> bool:
    """True when ``value`` normalises to a DOI."""
    return bool(normalize_doi(value))


def wire_doi(value: str | None) -> str:
    """Strip the resolver prefix / ``doi:`` and citation punctuation, keep case.

    This is the string handed to an API; :func:`normalize_doi` remains the
    dedupe key — same input, opposite casing policy (D3): DOIs are
    case-insensitive for identity, but they are not "fixed" on the wire.
    Returns ``""`` when ``value`` is not a DOI at all, so callers can branch on
    truthiness.
    """
    if not value:
        return ""
    text = _strip_prefixes(str(value), _DOI_PREFIXES).rstrip(".,;")
    return text if is_doi(text) else ""


def normalize_arxiv_id(value: str | None, *, keep_version: bool = False) -> str:
    """Return a bare arXiv id, without the ``arXiv:`` prefix or URL.

    Version suffixes (``v1``, ``v2``) are stripped unless ``keep_version`` —
    versions are revisions of the same work, so they must not split a match.
    """
    if not value:
        return ""
    text = _strip_prefixes(str(value), _ARXIV_PREFIXES)
    text = text.strip()
    # ``abs/2103.00020v1`` and ``pdf/2103.00020v1`` survive a partial strip.
    for segment in ("abs/", "pdf/"):
        if segment in text and text.split(segment, 1)[0].rstrip("/").endswith("arxiv.org"):
            text = text.split(segment, 1)[1]
    text = text.split("?", 1)[0].split("#", 1)[0].strip().rstrip("/")
    # A trailing ``.pdf`` is a filename, not part of the id.
    if text.lower().endswith(".pdf"):
        text = text[:-4]

    if not keep_version:
        text = _ARXIV_VERSION.sub("", text)

    if _CURRENT_ARXIV.match(text) or _LEGACY_ARXIV.match(text):
        return text
    # Anything left over is not an arXiv id; hand it back stripped so callers
    # can decide whether to treat it as one.
    return text


def is_arxiv_id(value: str | None) -> bool:
    """True when ``value`` looks like a current or legacy arXiv id."""
    bare = normalize_arxiv_id(value)
    return bool(bare) and bool(
        _CURRENT_ARXIV.match(bare) or _LEGACY_ARXIV.match(bare)
    )


def normalize_pmid(value: str | None) -> str:
    """Return a bare numeric PubMed id, or ``""``.

    Accepts ``"12345678"`` and the resolver form
    ``"https://pubmed.ncbi.nlm.nih.gov/12345678/"``.
    """
    if not value:
        return ""
    text = str(value).strip()
    for prefix in (
        "https://pubmed.ncbi.nlm.nih.gov/",
        "http://pubmed.ncbi.nlm.nih.gov/",
        "https://www.ncbi.nlm.nih.gov/pubmed/",
        "pmid:",
        "PMID:",
    ):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):]
            break
    text = text.strip().strip("/")
    return text if text.isdigit() else ""


def is_pmid(value: str | None) -> bool:
    """True when ``value`` normalises to a PMID."""
    return bool(normalize_pmid(value))


def normalize_title(value: str | None) -> str:
    """Fold a title to a case- and punctuation-insensitive match key.

    Used only for de-duplication when no stable identifier survives: two
    records that agree on this string *and* on year are treated as the same
    work.
    """
    if not value:
        return ""
    text = str(value).lower().replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def normalize_orcid(value: str | None) -> str:
    """Return a bare ORCID (``0000-0002-1825-0097``) or ``""``."""
    if not value:
        return ""
    text = str(value).strip()
    for prefix in ("https://orcid.org/", "http://orcid.org/", "orcid.org/", "orcid:"):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):]
            break
    text = text.strip().strip("/")
    # Upper-case *before* validating so a lower-case checksum digit (the only
    # letter in an ORCID) still matches and yields the canonical form.
    text = text.upper()
    return text if re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", text) else ""


def year_from_date(value: str | None) -> int | None:
    """Pull a four-digit year out of an ISO date or a free-text date.

    Handles ``"2021-03-05"``, ``"2021-03"``, ``"2021"``, and the MEDLINE
    form ``"2003 Jan-Feb"``.

    The year must *start* the string (after optional whitespace) and not be
    followed by another digit, so identifier-shaped input — ``"10.1000/xyz"``,
    ``"1706.03762"``, ``"12345678"`` — can never yield a year.  That is the
    guard this function has always claimed to enforce.
    """
    if not value:
        return None
    match = re.match(r"\s*(\d{4})(?!\d)", str(value))
    if not match:
        return None
    year = int(match.group(1))
    # Guard against parsing a page number or a date fragment as a year.
    return year if 1000 <= year <= 2999 else None


__all__ = [
    "is_arxiv_id",
    "is_doi",
    "is_pmid",
    "normalize_arxiv_id",
    "normalize_doi",
    "normalize_orcid",
    "normalize_pmid",
    "normalize_title",
    "wire_doi",
    "year_from_date",
]
