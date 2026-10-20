"""Local relevance scoring — the ranking signal the sources do not give you.

**Why this module exists.** Every source ranks by its own private score, and
none of those scores are comparable across sources: OpenAlex's ``relevance_score``
and Crossref's ordering are both integers in an undocumented scale.  The
pipeline's round-robin merge therefore interleaves positions, not quality — a
source's tenth-best hit lands next to another source's best hit.  That is why
one merged search can return a cardiology paper alongside a paper on AI
liability: each source genuinely believes its hit matched *its own* query
expansion, and nothing downstream ever says otherwise.

This module adds a **cross-source, local** signal: it scores each merged record
against the query text the caller actually typed, so a result set can be ranked
and filtered on one shared yardstick.

Three deliberate properties:

* **Pure.** Scoring reads only :class:`~researcher_python.models.Paper` fields
  and the query string.  No clock, no network, no randomness — the same record
  and query always yield the same float, so a saved result file stays
  reproducible.
* **Topical only.** Impact signals (citations, open access) are *deliberately*
  excluded.  Folding them in would make "relevance" drift as citation counts
  drift, and would quietly change a screened result set between runs.  Use
  ``sort="citations"`` for impact — it is a separate, explicit axis.
* **Additive, not a replacement.**  Nothing here overrides ``sort="relevance"``
  (the source-faithful default) — it adds ``sort="score"`` and an optional
  ``min_score`` floor alongside it.

Scoring shape, in ``[0.0, 1.0]``:

    score = 0.75 * coverage + 0.25 * density + phrase_bonus      (clamped)

* ``coverage`` — the fraction of query terms the record mentions at all.  This
  dominates, so a record hitting every term beats one repeating a single term
  a hundred times.  It is what separates "on topic" from "shares a word".
* ``density`` — how heavily the record is saturated with those terms relative
  to its length, saturating rather than growing without bound.
* ``phrase_bonus`` — a small bonus when the query appears as a phrase in the
  title, which is the cheapest strong signal of genuine topical match.

    >>> import researcher_python as rp
    >>> a = rp.Paper(title="Liability for artificial intelligence in healthcare")
    >>> b = rp.Paper(title="Global burden of cardiovascular disease")
    >>> query = "legal liability of artificial intelligence"
    >>> round(rp.score_paper(a, rp.query_terms(query)), 2) > round(
    ...     rp.score_paper(b, rp.query_terms(query)), 2
    ... )
    True
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from .models import Paper

__all__ = [
    "DEFAULT_MIN_SCORE",
    "DEFAULT_TOP_RATIO",
    "STOPWORDS",
    "filter_by_score",
    "query_terms",
    "score_paper",
    "score_papers",
]

#: Words carrying no topical signal.  Kept deliberately small and generic: an
#: aggressive list would strip meaningful terms from short queries like
#: "AI regulation", where "regulation" *is* the topic.
STOPWORDS: frozenset[str] = frozenset(
    """
    a an the and or but if then than that this these those there here
    of in on at to for from by with without within into onto over under
    is are was were be been being am do does did doing have has had having
    as it its it's they them their we our you your i me my he she his her
    not no nor so such can could may might must shall should will would
    how what which who whom whose when where why while about between during
    each few more most other some any both all one two also just only very
    using use used uses via per new study studies research paper article
    """.split()
)

#: Per-field weights.  A title match is worth roughly three abstract matches:
#: an author puts the topic in the title deliberately, and puts everything
#: else in the abstract.
FIELD_WEIGHTS: dict[str, float] = {
    "title": 3.0,
    "keywords": 1.5,
    "categories": 1.0,
    "abstract": 1.0,
    "venue": 0.5,
}

#: Weight of a full-phrase title hit, added on top of ``coverage``/``density``.
_PHRASE_BONUS = 0.10
#: Score above which a record is kept when a caller passes ``min_score``.
#: A gentle floor: it removes records sharing a single incidental term while
#: keeping anything that addresses most of the query.  ``None`` (the default
#: everywhere) means no floor at all.
DEFAULT_MIN_SCORE: float = 0.20

#: Suggested ``top_ratio`` — the value that separated signal from noise in
#: practice on a six-term LSEP query (39 merged records -> 31 kept, with every
#: dropped record being AGI-philosophy or unrelated-topic noise).
#:
#: **Raising it past ~0.6 starts removing genuine work.**  The same probe at
#: 0.6 dropped *The Malicious Use of AI* (0.521), which is squarely on topic —
#: a relative floor cannot tell "marginally relevant" from "topically
#: irrelevant", so an aggressive ratio trades real recall for a tidy list.
#: Scores tend to cluster with a gap after the weak tail, so a plateau (0.4
#: and 0.5 cut the *same* eight records on that query) is the signal that you
#: have found the natural break rather than cutting into signal.
DEFAULT_TOP_RATIO: float = 0.50

_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(text: object) -> list[str]:
    """Lowercase alphanumeric tokens — the unit of matching, punctuation-free."""
    return _WORD_RE.findall(str(text or "").lower())


def query_terms(query: str) -> list[str]:
    """Meaningful terms from a query, in order, de-duplicated.

    Stopwords and single characters are dropped, but *nothing else is*: an
    aggressive stemmer or synonym expansion would quietly change which records
    match, and the search has to stay reproducible.  Order is preserved so a
    phrase bonus reflects the query the caller actually typed.

    An empty result (a query of only stopwords, or empty) means "nothing to
    score against"; callers treat that as no signal rather than as zero.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for word in _words(query):
        if len(word) < 2 or word in STOPWORDS or word in seen:
            continue
        terms.append(word)
        seen.add(word)
    return terms


def _field_text(paper: Paper, field: str) -> str:
    """The text searched for one field, as one lowercased string."""
    if field == "title":
        return paper.title.lower()
    if field == "abstract":
        return paper.abstract.lower()
    if field == "venue":
        return paper.venue.lower()
    if field == "keywords":
        return " ".join(paper.keywords).lower()
    if field == "categories":
        return " ".join(paper.categories).lower()
    return ""


def _phrase_in_title(paper: Paper, terms: Sequence[str]) -> bool:
    """True when every term appears in the title *in order* (a real phrase)."""
    if len(terms) < 2:
        return False
    title_words = _words(paper.title)
    n = len(terms)
    for start in range(len(title_words) - n + 1):
        if title_words[start : start + n] == list(terms):
            return True
    return False


def score_paper(
    paper: Paper, terms: Sequence[str], fields: Sequence[str] | None = None
) -> float:
    """Relevance of one record to ``terms``, in ``[0.0, 1.0]``.

    Returns ``0.0`` when there are no terms to score against, or when the
    record matches none of them — so an unscored record and an irrelevant one
    are both clearly "not relevant", never silently plausible.

    ``fields`` restricts scoring to a subset of :data:`FIELD_WEIGHTS` (e.g.
    ``("title",)``).  This matters when the search was *scoped* to a field: a
    ``--field title`` search returns only title-matching records, so scoring
    them on title+abstract+keywords makes every one of them saturate at the
    same score — the score then carries no ranking information, ``--sort
    score`` becomes a no-op, and a relative floor can never cut anything.
    Scoring on the scoped field keeps the distinctions the caller asked for.

    Pure: same record + same terms (+ same fields) -> same value.
    """
    if not terms:
        return 0.0

    weights = FIELD_WEIGHTS if fields is None else {
        name: weight for name, weight in FIELD_WEIGHTS.items() if name in fields
    }
    if not weights:
        weights = FIELD_WEIGHTS

    # Coverage counts *distinct* terms matched; density measures how saturated
    # the text is with them.  Density is a weighted mean of per-field
    # saturation, not a sum: a sum saturates as soon as a title matches, which
    # would leave every title-scoped result on the same score.
    matched = 0
    density_weight = 0.0
    density_total = 0.0
    for field, weight in weights.items():
        text = _field_text(paper, field)
        if not text:
            continue
        words = _words(text)
        if not words:
            continue
        # Substring containment too, so "bias" still matches "biased".
        hits = sum(
            1
            for term in terms
            if term in words or any(term in word for word in words)
        )
        if not hits:
            continue
        matched = max(matched, hits)
        occurrences = sum(
            text.count(term) for term in terms
        )
        saturation = min(1.0, occurrences / len(words))
        density_total += weight * saturation
        density_weight += weight

    if not matched:
        return 0.0

    coverage = matched / len(terms)
    density = density_total / density_weight if density_weight else 0.0
    score = 0.75 * coverage + 0.25 * density
    if _phrase_in_title(paper, terms):
        score += _PHRASE_BONUS
    return max(0.0, min(1.0, round(score, 4)))


def score_papers(
    papers: Iterable[Paper], query: str, fields: Sequence[str] | None = None
) -> list[Paper]:
    """Stamp ``Paper.relevance`` for each record and return them in order.

    Input order is preserved — ranking is the caller's job
    (``sort_papers(..., "score")`` or :func:`filter_by_score`), so scoring can
    be applied to an already-screened, already-ordered result set without
    disturbing it.

    The score is written onto the record (rather than returned in a side dict)
    so it survives ``to_json``/``from_json`` and can become a Literature Matrix
    column — the screening decision and the score that justified it then travel
    together.

    ``fields`` restricts scoring to a subset of fields (see
    :func:`score_paper`); the pipeline passes the search's own field scope
    through, so a title-scoped search still ranks informatively.
    """
    terms = query_terms(query)
    items = list(papers)
    for paper in items:
        paper.relevance = score_paper(paper, terms, fields)
    return items


def filter_by_score(
    papers: Iterable[Paper],
    min_score: float | None = None,
    top_ratio: float | None = None,
) -> tuple[list[Paper], int]:
    """Split records into those worth keeping and those below the bar.

    Two independent floors, both optional and applied together:

    ``min_score``
        an **absolute** floor (0.0–1.0).  Simple, but it does not scale with
        query length: on a six-term query almost any on-subject paper matches
        two or three terms, so a low absolute floor can remove nothing at all.

    ``top_ratio``
        a **relative** floor (0.0–1.0) — keep records scoring at least
        ``top_ratio × the best score in the set``.  Scale-free, so it cuts a
        result set to its genuinely comparable top tier regardless of how many
        terms the query had.  This is usually the more useful of the two.

    Returns ``(kept, dropped)``.  With neither floor this keeps everything and
    reports ``0`` dropped, so callers can apply the logic unconditionally.

    Records whose relevance was never stamped (score ``None``) are **dropped**
    when either floor is set, because "unscored" cannot be shown to clear the
    bar — the same reason an unknown year is kept under a *date* filter only
    when no date filter was asked for.  :func:`score_papers` first, so every
    record carries a value.
    """
    items = list(papers)
    if min_score is None and top_ratio is None:
        return items, 0

    kept = list(items)
    if min_score is not None:
        kept = [p for p in kept if p.relevance is not None and p.relevance >= min_score]
    if top_ratio is not None:
        # Unscored records cannot clear a *relative* bar either, so they go
        # first — including when that empties the set, rather than leaving the
        # relative step a no-op that silently keeps everything.
        kept = [p for p in kept if p.relevance is not None]
        scored = [v for v in (p.relevance for p in kept) if v is not None]
        if scored:
            # Relative to the best *surviving* score, so the two floors compose:
            # the absolute floor removes junk, the relative one cuts the tail.
            cutoff = max(scored) * top_ratio
            kept = [p for p in kept if p.relevance is not None and p.relevance >= cutoff]
    return kept, len(items) - len(kept)