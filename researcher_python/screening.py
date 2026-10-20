"""Screening primitives: batch labels and rule-based pre-screening.

The library already stores a reader's judgement on a record
(:class:`~researcher_python.models.Paper.label`) and the workspace persists it.
What it did not have is a *cheap* way to apply many judgements, or a way to
spend judgement only on the ambiguous middle.

Two pure functions live here, so the CLI, the MCP server and an embedding
application cannot disagree about what a batch means or which records a rule
flags:

* :func:`apply_updates` applies many ``{id, label, reason, note}`` updates in
  one pass.  It indexes the record set once (all identifiers plus the exact
  title), validates every label *before* mutating anything, and reports
  unmatched ids instead of silently ignoring them.  Screening 1,000 records
  one call at a time is the cost this removes.
* :func:`prescreen` evaluates a small, ordered rule set — no title, no
  abstract, an excluded work type, a publication year outside a stated window
  — and returns *candidates*, not mutations.  The caller decides whether to
  apply them.  This is deliberately separate from reading a paper: a rule can
  be wrong for grey literature, so the default is a report.

Nothing here performs I/O; :class:`~researcher_python.workspace.Workspace`
owns the lock and the write.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import ConfigurationError
from .models import TRIAGE_LABELS, Paper
from .work_types import normalize_work_type

__all__ = [
    "DEFAULT_EXCLUDED_TYPES",
    "DEFAULT_PRESCREEN_RULES",
    "LabelUpdate",
    "PrescreenCriteria",
    "PrescreenHit",
    "PrescreenReport",
    "UpdateOutcome",
    "apply_updates",
    "index_papers",
    "prescreen",
]


# ---------------------------------------------------------------------- #
# Matching
# ---------------------------------------------------------------------- #


def _keys_for(paper: Paper) -> list[str]:
    """Every key an update may use to address ``paper``.

    ``primary_id()`` is only the *best* identifier, so a caller holding a PMID
    for a record whose DOI sorted first would miss it.  The index therefore
    contains each identifier separately, plus the exact title (the same
    fallback the single-record label path has always accepted).
    """
    keys: list[str] = [key for key in paper.merge_keys()]
    values = [
        paper.doi,
        paper.arxiv_id,
        paper.pmid,
        paper.pmcid,
        paper.isbn,
        paper.primary_id(),
        paper.title,
    ]
    # A promoted record's canonical DOI is the published one, but a saved
    # screening decision may still name the preprint.  Index every version's
    # bare identifier as well as the prefixed merge key, so both resolve.
    for version in paper.versions:
        values.extend(
            (version.doi, version.arxiv_id, version.pmid, version.pmcid)
        )
    for value in values:
        text = str(value or "").strip()
        if text:
            keys.append(text)
    # Preserve first-seen order while dropping duplicates.
    return list(dict.fromkeys(keys))


def index_papers(papers: Iterable[Paper]) -> dict[str, list[Paper]]:
    """Index records by every identifier and exact title.

    A key maps to a *list* because a bad source (or an import) can leave two
    records sharing a key.  Updating all of them is safer than picking one:
    the workspace merge normally makes this impossible, but a standalone file
    is not guaranteed deduped.
    """
    index: dict[str, list[Paper]] = {}
    for paper in papers:
        for key in _keys_for(paper):
            index.setdefault(key, []).append(paper)
    return index


# ---------------------------------------------------------------------- #
# Batch labels
# ---------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LabelUpdate:
    """One record's screening decision (or a dated note).

    ``id`` and ``ids`` are alternatives: a single identifier is the common
    case, while ``ids`` applies the same label to a group without repeating
    the update.  ``label=None`` means "leave the label alone, append the
    note" — the note-only path the single-record command already supports.
    """

    id: str | None = None
    ids: tuple[str, ...] = ()
    label: str | None = None
    reason: str = ""
    note: str = ""
    #: Explicit "every record in the set" — the batch equivalent of the
    #: single-record command's default when no ``--id`` is given.  Kept as a
    #: flag rather than an empty ``ids`` so "apply to nothing" and "apply to
    #: everything" cannot be confused.
    all_records: bool = False

    def identifiers(self) -> list[str]:
        """The identifiers this update targets, in order, without blanks."""
        found: list[str] = (
            [self.ids] if isinstance(self.ids, str) else [str(item) for item in self.ids]
        )
        if self.id is not None:
            found.insert(0, str(self.id))
        return [item.strip() for item in found if item.strip()]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LabelUpdate":
        """Accept the documented batch shape and a few obvious aliases."""
        if not isinstance(data, Mapping):
            raise ConfigurationError(
                f"a label update must be an object, got {type(data).__name__}"
            )
        raw_ids = data.get("ids")
        if isinstance(raw_ids, str):
            ids = (raw_ids,)
        elif isinstance(raw_ids, Iterable) and not isinstance(raw_ids, Mapping):
            ids = tuple(str(item) for item in raw_ids)
        else:
            ids = ()
        raw_id = data.get("id", data.get("identifier", data.get("doi")))
        raw_label = data.get("label", data.get("set_label"))
        return cls(
            id=str(raw_id) if raw_id is not None else None,
            ids=ids,
            label=str(raw_label) if raw_label is not None else None,
            reason=str(data.get("reason") or ""),
            note=str(data.get("note") or ""),
            all_records=bool(data.get("all") or data.get("all_records") or False),
        )

    def validate(self) -> str | None:
        """Why this update is unusable, or ``None`` when it is well formed."""
        if not self.all_records and not self.identifiers():
            return "no id (give id or ids)"
        if self.label is not None and self.label not in TRIAGE_LABELS:
            return (
                f"unknown triage label {self.label!r}; expected one of "
                f"{', '.join(TRIAGE_LABELS)}"
            )
        if self.label is None and not self.note:
            return "nothing to record (give label and/or note)"
        return None


@dataclass(slots=True)
class UpdateOutcome:
    """Result of :func:`apply_updates` — counts and the failures worth naming."""

    labelled: int = 0
    total: int = 0
    updated: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    invalid: list[dict[str, Any]] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "labelled": self.labelled,
            "total": self.total,
            "updated": list(self.updated),
            "unmatched": list(self.unmatched),
            "invalid": list(self.invalid),
            "labels": list(self.labels),
        }


def _coerce(update: LabelUpdate | Mapping[str, Any], index: int) -> LabelUpdate:
    try:
        return update if isinstance(update, LabelUpdate) else LabelUpdate.from_dict(update)
    except ConfigurationError as exc:
        raise ConfigurationError(f"update {index}: {exc}") from exc


def apply_updates(
    papers: Iterable[Paper],
    updates: Iterable[LabelUpdate | Mapping[str, Any]],
    *,
    strict: bool = True,
) -> UpdateOutcome:
    """Apply many label/note updates in one pass, returning what happened.

    Every update is validated before the first record is touched, so a batch
    with one typo cannot leave the set half-labelled.  With ``strict=False``
    the valid updates still apply and the bad ones are reported in
    :attr:`UpdateOutcome.invalid` — useful for an agent that would rather make
    progress than abandon a 1,000-record batch.

    An identifier that matches no record is *not* an error (the caller may be
    screening a merged file and an id can legitimately disappear); it is
    reported in :attr:`UpdateOutcome.unmatched` so the caller can see the
    difference between "applied silently" and "typo".
    """
    items = list(papers)
    index = index_papers(items)
    outcome = UpdateOutcome(total=len(items))

    prepared: list[tuple[int, LabelUpdate]] = []
    for position, raw in enumerate(updates):
        update = _coerce(raw, position)
        problem = update.validate()
        if problem is not None:
            outcome.invalid.append(
                {"index": position, "id": update.id, "error": problem}
            )
            if strict:
                raise ConfigurationError(f"update {position}: {problem}")
            continue
        prepared.append((position, update))

    touched: set[int] = set()
    for _, update in prepared:
        matched: list[Paper] = list(items) if update.all_records else []
        if not update.all_records:
            for identifier in update.identifiers():
                matched.extend(index.get(identifier, ()))
        # A record can be addressed twice (say by DOI and title) in one batch;
        # apply it once so the count is records, not keys.
        seen: set[int] = set()
        for paper in matched:
            if id(paper) in seen:
                continue
            seen.add(id(paper))
            if update.label is not None:
                paper.label_as(update.label, update.reason)
                if update.label not in outcome.labels:
                    outcome.labels.append(update.label)
            if update.note:
                paper.note(update.note)
            touched.add(id(paper))
        if not matched and not update.all_records:
            for identifier in update.identifiers():
                if identifier not in outcome.unmatched:
                    outcome.unmatched.append(identifier)
        else:
            for identifier in update.identifiers():
                if identifier not in outcome.updated:
                    outcome.updated.append(identifier)

    # ``labelled`` counts records, not updates: two entries for one paper are
    # one screening decision (the later one wins).
    outcome.labelled = len(touched)
    return outcome


# ---------------------------------------------------------------------- #
# Pre-screening
# ---------------------------------------------------------------------- #

#: Work types a screening pass can rule out without reading the record.  The
#: caller can replace this list; the default is the one the coursework gap
#: named (grants, standards and datasets are not the paper you are reviewing).
DEFAULT_EXCLUDED_TYPES: tuple[str, ...] = ("grant", "standard", "dataset")

#: Rules in evaluation order.  Stable names are part of the report contract.
DEFAULT_PRESCREEN_RULES: tuple[str, ...] = (
    "no-title",
    "no-abstract",
    "type",
    "outside-year",
)


def _fold(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


@dataclass(frozen=True, slots=True)
class PrescreenCriteria:
    """The rules and bounds a pre-screen evaluates.

    ``year_from``/``year_to`` are optional.  A record whose year is unknown is
    **kept** under an ``outside-year`` rule, matching the search filters: an
    unknown year is not evidence that a record is out of scope.  The same
    applies to an empty work type.
    """

    year_from: int | None = None
    year_to: int | None = None
    exclude_types: tuple[str, ...] = DEFAULT_EXCLUDED_TYPES
    require_abstract: bool = True
    require_title: bool = True
    rules: tuple[str, ...] | None = None

    def selected_rules(self) -> tuple[str, ...]:
        if self.rules is None:
            return DEFAULT_PRESCREEN_RULES
        unknown = [name for name in self.rules if name not in DEFAULT_PRESCREEN_RULES]
        if unknown:
            raise ConfigurationError(
                f"unknown pre-screen rule(s) {', '.join(unknown)}; "
                f"expected one of {', '.join(DEFAULT_PRESCREEN_RULES)}"
            )
        return tuple(self.rules)


@dataclass(slots=True)
class PrescreenHit:
    """One record the rules flagged, with the reasons it was flagged."""

    paper: Paper
    reasons: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


@dataclass(slots=True)
class PrescreenReport:
    """What a pre-screen found.  ``hits`` are candidates, never decisions."""

    hits: list[PrescreenHit] = field(default_factory=list)
    considered: int = 0
    skipped_labelled: int = 0
    counts_by_rule: dict[str, int] = field(default_factory=dict)

    @property
    def flagged(self) -> int:
        return len(self.hits)

    def as_dict(self, *, limit: int | None = None) -> dict[str, Any]:
        shown = self.hits if limit is None else self.hits[:limit]
        return {
            "considered": self.considered,
            "flagged": self.flagged,
            "skipped_labelled": self.skipped_labelled,
            "by_rule": dict(sorted(self.counts_by_rule.items())),
            "candidates": [
                {
                    "id": hit.paper.primary_id(),
                    "title": hit.paper.title,
                    "year": hit.paper.year,
                    "reasons": list(hit.reasons),
                }
                for hit in shown
            ],
            "truncated": max(0, self.flagged - len(shown)),
        }


def _rule_no_title(paper: Paper, criteria: PrescreenCriteria) -> str | None:
    if criteria.require_title and not _fold(paper.title):
        return "no title"
    return None


def _rule_no_abstract(paper: Paper, criteria: PrescreenCriteria) -> str | None:
    if criteria.require_abstract and not _fold(paper.abstract):
        return "no abstract"
    return None


def _rule_type(paper: Paper, criteria: PrescreenCriteria) -> str | None:
    # Canonicalise both sides so "Journal Article" and "journal-article" are
    # the same type, exactly as the search filter treats them.
    value = normalize_work_type(paper.work_type)
    if not value:
        return None
    excluded = {
        canonical
        for item in criteria.exclude_types
        if (canonical := normalize_work_type(item))
    }
    if value in excluded:
        return f"type: {value}"
    return None


def _rule_outside_year(paper: Paper, criteria: PrescreenCriteria) -> str | None:
    if paper.year is None:
        return None
    low, high = criteria.year_from, criteria.year_to
    if low is None and high is None:
        return None
    if low is not None and paper.year < low:
        if high is not None:
            return f"outside year window ({paper.year} not in {low}-{high})"
        return f"outside year window ({paper.year} < {low})"
    if high is not None and paper.year > high:
        if low is not None:
            return f"outside year window ({paper.year} not in {low}-{high})"
        return f"outside year window ({paper.year} > {high})"
    return None


_RULES = {
    "no-title": _rule_no_title,
    "no-abstract": _rule_no_abstract,
    "type": _rule_type,
    "outside-year": _rule_outside_year,
}


def prescreen(
    papers: Iterable[Paper],
    criteria: PrescreenCriteria | None = None,
    *,
    skip_labelled: bool = True,
) -> PrescreenReport:
    """Flag records that a cheap rule can exclude, without changing them.

    A record already carrying a human label is skipped by default: the point
    is to make the *ambiguous middle* cheap, not to overwrite a decision.  A
    record can collect several reasons; they are reported together so the
    caller can see why a candidate was flagged without re-deriving it.

    The rule set is ordered and named, and the counts are reported per rule —
    that is what lets a PRISMA-style appendix say "N excluded for no abstract"
    instead of a single opaque exclusion count.
    """
    criteria = criteria or PrescreenCriteria()
    rules = criteria.selected_rules()
    report = PrescreenReport(counts_by_rule={name: 0 for name in rules})

    for paper in papers:
        if skip_labelled and paper.triage_label() != "unreviewed":
            report.skipped_labelled += 1
            continue
        report.considered += 1
        reasons: list[str] = []
        for name in rules:
            reason = _RULES[name](paper, criteria)
            if reason:
                reasons.append(reason)
                report.counts_by_rule[name] += 1
        if reasons:
            report.hits.append(PrescreenHit(paper=paper, reasons=reasons))

    return report
