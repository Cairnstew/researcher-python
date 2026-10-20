"""One vocabulary for work types, across every source.

Each API names the same kind of thing differently: OpenAlex says ``article``,
Crossref ``journal-article``, PubMed ``Journal Article``, Semantic Scholar
``JournalArticle``.  A type filter that compared those strings literally would
be unusable across a merged result set, so every type entering or leaving the
pipeline passes through :func:`normalize_work_type`.

The canonical vocabulary is deliberately small.  It answers the question a
literature search actually asks — "is this a paper, a preprint, a book, a
grant?" — not "which of forty Crossref subtypes is it?".  Unrecognised values
normalise to ``""`` and are **kept** by an exclusion filter: an unknown type is
not evidence that a record is a grant, just as an unknown year is not evidence
that it is out of range.

Two policies are built on this module:

* :data:`DEFAULT_SEARCH_EXCLUDE_TYPES` — searches drop grants and standards by
  default.  Datasets are deliberately *not* here: a dataset can be a legitimate
  hit, and ``screening.prescreen`` already flags one at the screening stage,
  where the decision is visible and logged.
* ``screening`` reuses :func:`normalize_work_type`, so a pre-screen rule and a
  search filter agree about ``Journal Article`` vs ``journal-article``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = [
    "CANONICAL_WORK_TYPES",
    "DEFAULT_SEARCH_EXCLUDE_TYPES",
    "is_known_work_type",
    "normalize_work_type",
    "resolve_types",
]

#: The closed vocabulary a caller may ask for.  ``other`` is an explicit
#: canonical bucket for a source that says "other"; an *unrecognised* value is
#: ``""``, not ``other``.
CANONICAL_WORK_TYPES: tuple[str, ...] = (
    "article",
    "review",
    "preprint",
    "book",
    "book-chapter",
    "proceedings-article",
    "thesis",
    "report",
    "dataset",
    "grant",
    "standard",
    "editorial",
    "letter",
    "erratum",
    "peer-review",
    "paratext",
    "other",
)

#: Search wins by default.  See the module docstring for why datasets are not
#: included (prescreen catches them later, visibly).
DEFAULT_SEARCH_EXCLUDE_TYPES: tuple[str, ...] = ("grant", "standard")

#: Native spellings, folded.  A value absent from this table is *unknown*.
_ALIASES: dict[str, str] = {
    # -- articles -------------------------------------------------------
    "article": "article",
    "journal article": "article",
    "journal-article": "article",
    "journalarticle": "article",
    "research article": "article",
    "original article": "article",
    "case report": "article",
    "casereport": "article",
    "clinical trial": "article",
    "study": "article",
    "news": "article",
    # -- reviews --------------------------------------------------------
    "review": "review",
    "systematic review": "review",
    "literature review": "review",
    "scoping review": "review",
    "narrative review": "review",
    "meta analysis": "review",
    "meta-analysis": "review",
    "metaanalysis": "review",
    # -- preprints ------------------------------------------------------
    "preprint": "preprint",
    "posted content": "preprint",
    "posted-content": "preprint",
    # -- books ----------------------------------------------------------
    "book": "book",
    "monograph": "book",
    "edited book": "book",
    "reference book": "book",
    "textbook": "book",
    "book set": "book",
    "book-set": "book",
    # -- book chapters / entries ---------------------------------------
    "book chapter": "book-chapter",
    "book-chapter": "book-chapter",
    "bookchapter": "book-chapter",
    "booksection": "book-chapter",
    "book section": "book-chapter",
    "book-section": "book-chapter",
    "book part": "book-chapter",
    "book-part": "book-chapter",
    "chapter": "book-chapter",
    "reference entry": "book-chapter",
    "reference-entry": "book-chapter",
    # -- conference / proceedings ---------------------------------------
    "proceedings article": "proceedings-article",
    "proceedings-article": "proceedings-article",
    "conference paper": "proceedings-article",
    "conference-paper": "proceedings-article",
    "conferencepaper": "proceedings-article",
    "conference": "proceedings-article",
    "conference abstract": "proceedings-article",
    # -- theses / reports -----------------------------------------------
    "thesis": "thesis",
    "dissertation": "thesis",
    "doctoral thesis": "thesis",
    "masters thesis": "thesis",
    "report": "report",
    "technical report": "report",
    "research report": "report",
    "report series": "report",
    "report-series": "report",
    "working paper": "report",
    "software": "report",
    # -- data / funding / standards -------------------------------------
    "dataset": "dataset",
    "data set": "dataset",
    "database": "dataset",
    "supplementary materials": "dataset",
    "supplementary-materials": "dataset",
    "grant": "grant",
    "award": "grant",
    "funding": "grant",
    "standard": "standard",
    "standard series": "standard",
    "standard-series": "standard",
    "specification": "standard",
    # -- editorial matter -----------------------------------------------
    "editorial": "editorial",
    "letter": "letter",
    "letters and comments": "letter",
    "lettersandcomments": "letter",
    "comment": "letter",
    "erratum": "erratum",
    "correction": "erratum",
    "retraction": "erratum",
    "retracted publication": "erratum",
    "peer review": "peer-review",
    "peer-review": "peer-review",
    "paratext": "paratext",
    "paratexts": "paratext",
    # -- explicit other -------------------------------------------------
    "other": "other",
    "libguides": "other",
    "website": "other",
    "text": "other",
}


def _fold(value: object) -> str:
    """Case- and punctuation-insensitive form, without collapsing hyphens.

    ``Journal Article`` -> ``journal article``; ``book-chapter`` ->
    ``book-chapter`` (the alias table keeps its own hyphenated spellings).
    """
    text = " ".join(str(value or "").split()).casefold().replace("&", " and ")
    text = re.sub(r"[^\w\s-]", " ", text)
    return " ".join(text.split())


def normalize_work_type(value: object) -> str:
    """Map a source-native type onto :data:`CANONICAL_WORK_TYPES`, or ``""``.

    Already-canonical values pass through.  An unrecognised value is ``""``,
    never a guessed bucket — a wrong guess would hide a record from a type
    filter on evidence that does not exist.
    """
    folded = _fold(value)
    if not folded:
        return ""
    return _ALIASES.get(folded, "")


def is_known_work_type(value: object) -> bool:
    """True when ``value`` resolves to a canonical type."""
    return bool(normalize_work_type(value))


def resolve_types(values: Iterable[str] | str) -> tuple[str, ...]:
    """Validate and canonicalise a caller's type list.

    Accepts canonical names and every alias in the table, so
    ``--type journal-article`` and ``--type article`` mean the same thing.
    Raises :class:`ValueError` naming the legal values for anything else — a
    typo in a filter should fail loudly, not silently match nothing.
    """
    if isinstance(values, str):
        values = (values,)
    resolved: list[str] = []
    unknown: list[str] = []
    for raw in values:
        for part in str(raw or "").split(","):
            text = part.strip()
            if not text:
                continue
            canonical = normalize_work_type(text)
            if not canonical:
                unknown.append(text)
            elif canonical not in resolved:
                resolved.append(canonical)
    if unknown:
        raise ValueError(
            f"unknown work type(s) {', '.join(repr(x) for x in unknown)}; "
            f"expected one of {', '.join(CANONICAL_WORK_TYPES)} "
            "(source spellings such as 'journal-article' are also accepted)"
        )
    return tuple(resolved)
