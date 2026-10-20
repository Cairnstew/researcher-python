"""PRISMA-style screening counts derived from the workspace log and labels.

A screening appendix has to answer more than "how many did I keep".  PRISMA's
flow is *identification -> duplicates removed -> screened -> included*, and
every number in it already exists in a workspace: the append-only search log
records how many records each retrieval returned (and per source), while the
reader-owned labels record the screening decisions.

This module joins the two.  It is deliberately pure — pass the papers and the
log entries — so the same report can be rendered for a CLI, an MCP reply or a
coursework appendix without touching the store.

Two honesty rules matter more than the formatting:

* **A gap is reported, not smoothed.**  If a workspace was imported without a
  log, or holds records no search row accounts for, ``unaccounted`` says so and
  ``warnings`` names it.  A PRISMA diagram with invented identification counts
  is worse than one that admits it cannot reconstruct them.
* **Only the stages this package models are claimed.**  Screening here is
  title/abstract plus citation snowballing; there is no full-text eligibility
  stage, so the report says that rather than drawing a four-stage PRISMA
  diagram it cannot support.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .models import TRIAGE_LABELS, Paper

__all__ = ["prisma_report", "render_prisma_markdown"]


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _entry_identified(entry: Mapping[str, Any]) -> int:
    """Records one log row identified, before duplicate removal.

    ``by_source`` is the raw per-source count the pipeline recorded *before*
    merging, which is exactly what PRISMA calls "records identified from each
    database".  A legacy row or an expansion may not have it, so fall back to
    the post-merge ``returned``/``kept`` count rather than counting zero.
    """
    by_source = entry.get("by_source") or {}
    if isinstance(by_source, Mapping) and by_source:
        return sum(_as_int(count) for count in by_source.values())
    outcome = entry.get("outcome") or {}
    if isinstance(outcome, Mapping):
        return _as_int(outcome.get("returned", outcome.get("kept", 0)))
    return 0


def prisma_report(
    papers: Iterable[Paper],
    log_entries: Iterable[Mapping[str, Any]],
    *,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the flow counts from a record set and its search log."""
    items = list(papers)
    entries = list(log_entries)
    provenance = dict(provenance or {})

    counts: dict[str, int] = {label: 0 for label in TRIAGE_LABELS}
    excluded_by_reason: dict[str, int] = {}
    prescreen_excluded = 0
    for paper in items:
        label = paper.triage_label()
        counts[label] += 1
        if label != "excluded":
            continue
        reason = " ".join((paper.exclusion_reason or "").split()) or "(no reason given)"
        excluded_by_reason[reason] = excluded_by_reason.get(reason, 0) + 1
        if reason.startswith("pre-screen:"):
            prescreen_excluded += 1

    by_source: dict[str, int] = {}
    by_channel: dict[str, int] = {}
    identified = 0
    identified_merged = 0
    type_excluded = 0
    type_excluded_by_type: dict[str, int] = {}
    rounds: set[int] = set()
    for entry in entries:
        count = _entry_identified(entry)
        identified += count
        merged = entry.get("outcome") or {}
        if isinstance(merged, Mapping):
            identified_merged += _as_int(merged.get("returned", merged.get("kept", 0)))
            type_excluded += _as_int(merged.get("dropped_types", 0))
            raw_types = merged.get("dropped_type_counts") or {}
            if isinstance(raw_types, Mapping):
                for name, value in raw_types.items():
                    type_excluded_by_type[str(name)] = (
                        type_excluded_by_type.get(str(name), 0) + _as_int(value)
                    )
        channel = str(entry.get("channel") or "search")
        by_channel[channel] = by_channel.get(channel, 0) + count
        raw = entry.get("by_source") or {}
        if isinstance(raw, Mapping):
            for name, value in raw.items():
                by_source[str(name)] = by_source.get(str(name), 0) + _as_int(value)
        rounds.add(_as_int(entry.get("round", 1)))

    deduped = len(items)
    # A work-type exclusion is a screening-stage removal, not a duplicate:
    # subtract it before attributing the rest to dedupe, so "identified"
    # reconciles with "deduped" without hiding the policy.
    duplicates_removed = max(0, identified - deduped - type_excluded)
    unaccounted = max(0, deduped + type_excluded - identified)
    unreviewed = counts["unreviewed"]
    excluded = counts["excluded"]
    core = counts["core"]
    general = counts["general"]
    included = core + general
    screened = deduped - unreviewed
    ready = deduped > 0 and unreviewed == 0

    warnings: list[str] = []
    if deduped and not entries:
        warnings.append(
            "no search log rows: identification counts cannot be reconstructed; "
            "every record is unaccounted"
        )
    elif unaccounted:
        warnings.append(
            f"{unaccounted} record(s) have no identification row; the flow is "
            "reconstructed from an import and may be incomplete"
        )
    if provenance.get("legacy"):
        warnings.append(
            "legacy import: identification counts came from imported log rows "
            "and may not cover every record"
        )
    if unreviewed:
        warnings.append(
            f"{unreviewed} of {deduped} record(s) are still 'unreviewed'; the "
            "screening stage is incomplete"
        )

    return {
        "identified": identified,
        "identified_merged": identified_merged,
        "duplicates_removed": duplicates_removed,
        "type_excluded": type_excluded,
        "type_excluded_by_type": dict(sorted(type_excluded_by_type.items())),
        "unaccounted": unaccounted,
        "deduped": deduped,
        "screened": screened,
        "excluded": excluded,
        "included": included,
        "core": core,
        "general": general,
        "unreviewed": unreviewed,
        "by_source": dict(sorted(by_source.items())),
        "by_channel": dict(sorted(by_channel.items())),
        "excluded_by_reason": dict(
            sorted(excluded_by_reason.items(), key=lambda item: (-item[1], item[0]))
        ),
        "prescreen_excluded": prescreen_excluded,
        "searches": len(entries),
        "rounds": sorted(rounds),
        "ready": ready,
        "warnings": warnings,
        "stages_not_modelled": (
            "full-text eligibility and study-level quality assessment are not "
            "modelled; counts describe title/abstract screening and snowballing"
        ),
    }


def _cell(value: Any) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def render_prisma_markdown(report: Mapping[str, Any]) -> str:
    """Render a PRISMA-style flow as Markdown for a report appendix."""
    d = {key: report.get(key, 0) for key in (
        "identified", "duplicates_removed", "type_excluded", "deduped",
        "screened", "excluded", "unreviewed", "included", "core", "general",
    )}
    lines = [
        "# PRISMA-style screening flow",
        "",
        "| Stage | n |",
        "| --- | --- |",
        f"| Records identified | {d['identified']} |",
        f"| Duplicates removed | {d['duplicates_removed']} |",
        f"| Work-type removals | {d['type_excluded']} |",
        f"| Records after dedupe and type filter | {d['deduped']} |",
        f"| Records screened | {d['screened']} |",
        f"| Records excluded | {d['excluded']} |",
        f"| Awaiting screening | {d['unreviewed']} |",
        f"| Included (core + general) | {d['included']} |",
        f"| — core | {d['core']} |",
        f"| — general | {d['general']} |",
        "",
    ]

    by_source = report.get("by_source") or {}
    if by_source:
        lines += ["## Identification by source", "", "| Source | Records |", "| --- | --- |"]
        lines += [f"| {_cell(name)} | {count} |" for name, count in by_source.items()]
        lines.append("")

    by_reason = report.get("excluded_by_reason") or {}
    if by_reason:
        lines += ["## Exclusions by reason", "", "| Reason | Records |", "| --- | --- |"]
        lines += [f"| {_cell(reason)} | {count} |" for reason, count in by_reason.items()]
        lines.append("")

    warnings = report.get("warnings") or []
    if warnings:
        lines += ["## Notes", ""]
        lines += [f"- {warning}" for warning in warnings]
        lines.append("")
    note = report.get("stages_not_modelled")
    if note:
        lines += ["## Scope", "", f"- {note}", ""]

    return "\n".join(lines).rstrip() + "\n"
