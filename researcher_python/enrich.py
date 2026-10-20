"""Best-effort metadata backfill from OpenAlex.

A merged result set routinely arrives with a citation count on only ~70% of
records, and ``matrix.citations_per_year`` is blank whenever either input is
missing.  OpenAlex indexes most DOIs and supports a true OR over DOI filters,
so the missing counts, abstracts and work types can be filled at roughly one
request per fifty records instead of one per record.

The backfill is deliberately conservative:

* **Missing only.**  A count from Crossref or Semantic Scholar is never
  overwritten; the record keeps the provenance of the number it already had.
  OpenAlex only fills ``citation_count`` when it is ``None``, and stamps
  ``citation_count_source="openalex"`` when it does.
* **Best effort.**  A failed batch is reported in :class:`EnrichmentReport`
  and never fails the search.  Records the API does not recognise are counted
  as ``missing`` and left exactly as they were.
* **Bounded.**  At most :data:`MAX_RECORDS` addressable records are looked up
  per search, in chunks of 50.

The same fetch also harvests OpenAlex ``locations`` and identifiers, which is
where explicit preprint/published links come from.  Enrichment therefore
precedes the version-link pass in the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ResearcherError
from .ids import arxiv_doi, normalize_doi
from .models import Paper, PaperVersion

__all__ = ["EnrichmentReport", "MAX_RECORDS", "enrich_papers", "citation_coverage"]

#: Cap on records enriched in one search.  A search can merge well over
#: ``max_results``; ten batches is enough to cover 500 records without turning
#: a search into a crawl.
MAX_RECORDS = 500


@dataclass(slots=True)
class EnrichmentReport:
    """What the backfill did, including the coverage it moved."""

    ok: bool = False
    skipped_reason: str = ""
    requested: int = 0
    matched: int = 0
    missing: int = 0
    skipped: int = 0
    citation_filled: int = 0
    abstract_filled: int = 0
    type_filled: int = 0
    reference_filled: int = 0
    versions_added: int = 0
    coverage_before: float = 0.0
    coverage_after: float = 0.0
    abstract_coverage_before: float = 0.0
    abstract_coverage_after: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "skipped_reason": self.skipped_reason,
            "requested": self.requested,
            "matched": self.matched,
            "missing": self.missing,
            "skipped": self.skipped,
            "citation_filled": self.citation_filled,
            "abstract_filled": self.abstract_filled,
            "type_filled": self.type_filled,
            "reference_filled": self.reference_filled,
            "versions_added": self.versions_added,
            "coverage_before": round(self.coverage_before, 4),
            "coverage_after": round(self.coverage_after, 4),
            "abstract_coverage_before": round(self.abstract_coverage_before, 4),
            "abstract_coverage_after": round(self.abstract_coverage_after, 4),
            "errors": list(self.errors),
        }


def citation_coverage(papers: list[Paper]) -> tuple[float, float]:
    """(citation coverage, abstract coverage) as fractions in ``[0, 1]``."""
    total = len(papers) or 1
    cites = sum(p.citation_count is not None for p in papers) / total
    abstracts = sum(bool(p.abstract.strip()) for p in papers) / total
    return cites, abstracts


def _address(paper: Paper) -> str:
    """The DOI worth asking OpenAlex about, or ``""``.

    A journal DOI is used as-is.  An arXiv-only record is addressed by its
    arXiv DOI (``10.48550/arxiv.X``), which OpenAlex resolves to the published
    work when one exists — that resolution is itself an explicit version link.
    """
    if paper.doi:
        return normalize_doi(paper.doi)
    return arxiv_doi(paper.arxiv_id)


def _keys(paper: Paper) -> set[str]:
    return set(paper.merge_keys())


def _match(
    enriched: Paper, by_key: dict[str, list[Paper]]
) -> list[Paper]:
    """Every candidate whose identity intersects this OpenAlex record."""
    hits: list[Paper] = []
    seen: set[int] = set()
    for key in _keys(enriched):
        for paper in by_key.get(key, ()):
            if id(paper) not in seen:
                seen.add(id(paper))
                hits.append(paper)
    return hits


def _merge_version(paper: Paper, enriched: Paper) -> bool:
    """Add the resolved OpenAlex record as a version entry when it is new."""
    doi = normalize_doi(enriched.doi)
    if not doi or doi == normalize_doi(paper.doi):
        return False
    entry = enriched.as_version()
    entry.source = "openalex"
    entry.relation = "resolved-doi"
    entry.asserted_by = "openalex"
    key = entry.key()
    if any(version.key() == key for version in paper.versions):
        return False
    paper.versions.append(entry)
    return True


def _fill(paper: Paper, enriched: Paper, report: EnrichmentReport) -> None:
    """Fill only the gaps named by the backfill contract."""
    if paper.citation_count is None and enriched.citation_count is not None:
        paper.citation_count = enriched.citation_count
        paper.citation_count_source = "openalex"
        report.citation_filled += 1
    if not paper.abstract.strip() and enriched.abstract.strip():
        paper.abstract = enriched.abstract
        report.abstract_filled += 1
    if not paper.work_type.strip() and enriched.work_type.strip():
        paper.work_type = enriched.work_type
        report.type_filled += 1
    if paper.reference_count is None and enriched.reference_count is not None:
        paper.reference_count = enriched.reference_count
        report.reference_filled += 1
    if enriched.ids.get("openalex"):
        paper.ids.setdefault("openalex", enriched.ids["openalex"])
    for version in enriched.versions:
        if all(existing.key() != version.key() for existing in paper.versions):
            paper.versions.append(version)
            report.versions_added += 1
    if _merge_version(paper, enriched):
        report.versions_added += 1


def enrich_papers(
    papers: list[Paper],
    source: Any,
    *,
    max_records: int = MAX_RECORDS,
) -> EnrichmentReport:
    """Backfill missing counts/abstracts/types from the participating OpenAlex.

    ``source`` is the :class:`~researcher_python.sources.openalex_source.
    OpenAlexSource` instance the search already constructed, or ``None``.  A
    source without the ``enrich`` capability is skipped, which is also what
    keeps the offline test fakes from making a network call.
    """
    report = EnrichmentReport()
    report.coverage_before, report.abstract_coverage_before = citation_coverage(papers)
    if source is None:
        report.skipped_reason = "openalex was not part of this search"
        report.coverage_after = report.coverage_before
        report.abstract_coverage_after = report.abstract_coverage_before
        return report
    if not getattr(source, "supports", lambda _op: False)("enrich"):
        report.skipped_reason = "the source has no batch enrichment capability"
        report.coverage_after = report.coverage_before
        report.abstract_coverage_after = report.abstract_coverage_before
        return report

    addressable: list[Paper] = []
    addresses: list[str] = []
    for paper in papers:
        address = _address(paper)
        if address:
            addressable.append(paper)
            addresses.append(address)
    report.requested = len(addressable)
    if not addressable:
        report.ok = True
        report.coverage_after = report.coverage_before
        report.abstract_coverage_after = report.abstract_coverage_before
        return report

    if len(addressable) > max_records:
        report.skipped = len(addressable) - max_records
        addressable = addressable[:max_records]
        addresses = addresses[:max_records]

    by_key: dict[str, list[Paper]] = {}
    for paper in addressable:
        for key in _keys(paper):
            by_key.setdefault(key, []).append(paper)

    try:
        enriched = list(source.works_by_dois(addresses))
    except ResearcherError as exc:
        report.errors.append(f"{type(exc).__name__}: {exc}")
        report.coverage_after = report.coverage_before
        report.abstract_coverage_after = report.abstract_coverage_before
        return report
    except Exception as exc:  # pragma: no cover - defensive; transport is wrapped
        report.errors.append(f"{type(exc).__name__}: {exc}")
        report.coverage_after = report.coverage_before
        report.abstract_coverage_after = report.abstract_coverage_before
        return report

    matched_ids: set[int] = set()
    for record in enriched:
        hits = _match(record, by_key)
        if not hits:
            continue
        for paper in hits:
            matched_ids.add(id(paper))
            _fill(paper, record, report)
    report.matched = len(matched_ids)
    report.missing = len(addressable) - report.matched
    report.ok = not report.errors
    report.coverage_after, report.abstract_coverage_after = citation_coverage(papers)
    return report
