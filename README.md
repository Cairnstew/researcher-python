# researcher-python

One Python pipeline across five scholarly sources: search several APIs in
parallel, merge the results into a single `Paper` record per work, export them
as JSON or JSON Lines, and download a PDF when one is open. The same five
sources are reachable from the library, from the `researcher-python` console
script, and from `python -m researcher_python`.

## Sources

| Source | search | get | references | citations | pdf | status |
|---|---|---|---|---|---|---|
| `openalex` | ✓ | ✓ | ✓ | ✓ | ✓ | live-verified |
| `semantic_scholar` | ✓ | ✓ | ✓ | ✓ | ✓ | implemented, not live-verified |
| `arxiv` | ✓ | ✓ | – | – | ✓ | live-verified |
| `crossref` | ✓ | ✓ | ✓ | – | – | implemented, not live-verified |
| `pubmed` | ✓ | ✓ | ✓ | ✓ | – | live-verified |


`pdf` means the adapter puts a direct open-access PDF URL on `Paper.pdf_url`.
`crossref` and `pubmed` resolve PDFs through Unpaywall by DOI instead.
"not live-verified" means the adapter is covered by offline fixtures but has
not been exercised against the real API from a non-rate-limited address yet.

### Opt-in sources

| name | search | get | references | citations | pdf | what it adds |
| --- | --- | --- | --- | --- | --- | --- |
| `acm` | yes | yes | yes | no | no | ACM Digital Library, scoped to the `10.1145` DOI prefix |
| `openaire` | yes | yes | no | no | no | repositories, theses and EU-funded project deliverables |

Both are **opt-in** (`--source acm`, `--source openaire`) and neither is in
`DEFAULT_SOURCES`, because each is a facet of Crossref's reach: adding them to
an unqualified search would inflate a merged result set with records the broad
sources already returned.

**`acm`** is the ACM Digital Library. The DL has no open API — its search
results are HTML — but every ACM item carries a `10.1145` DOI that Crossref
indexes, so the adapter is a prefix-scoped Crossref view: `--source acm` really
does search only the Digital Library, ranked by Crossref's relevance score
within it. **`openaire`** is the grey-literature source: the European
repository/thesis/deliverable aggregator, free and unauthenticated, with a
`--type` facet for theses and reports. Neither is a default source (they are
subsets of Crossref's reach); name them when the specialism is the point.

## Install / develop

The repo is Nix-first:

```bash
nix develop              # Python + all runtime deps + pytest on PATH
nix develop --command pytest tests/ -q
```

`pyproject.toml` is a standard setuptools build, so `pip install -e .` works
too if you prefer not to use Nix.

## Configuration

All settings come from the environment (or a `.env` file in the working
directory); `.env.example` documents every key with a one-line note on where
to get it.

| Variable | Required | Purpose |
|---|---|---|
| `RESEARCHER_EMAIL` | for `pubmed` | NCBI requires it; Crossref/Unpaywall polite pool |
| `RESEARCHER_SEMANTIC_SCHOLAR_API_KEY` | in practice | keyless calls are usually HTTP 429 |
| `RESEARCHER_OPENALEX_API_KEY` | optional | lifts the limit, stops the 503 search pauses |
| `RESEARCHER_NCBI_API_KEY` | optional | 3 → 10 requests/second |
| `RESEARCHER_STACKEXCHANGE_KEY` | optional | web-source quota (Stack Overflow etc.) |
| `RESEARCHER_REDDIT_CLIENT_ID` | reddit only | OAuth app id (anonymous Reddit JSON is 403) |
| `RESEARCHER_REDDIT_CLIENT_SECRET` | reddit only | OAuth app secret |
| `RESEARCHER_OUTPUT_DIR` | optional | where `download` writes PDFs (default `downloads`) |
| `RESEARCHER_LOG_LEVEL` | optional | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `RESEARCHER_MAX_RESULTS` | optional | per-source cap for one search (default 50) |
| `RESEARCHER_RETRIES` | optional | extra attempts after a 429 (default 1) |
| `RESEARCHER_ENRICH` | optional | backfill missing counts/abstracts from OpenAlex (default true) |

A missing `RESEARCHER_EMAIL` does not stop the library: it only surfaces when
a command actually needs it (building the `pubmed` source, or resolving a PDF
through Unpaywall).

## CLI

The installed console script is `researcher-python`. From a checkout use the
equivalent module form, `python -m researcher_python` — both run the same
`main()`.

```bash
# What is configured, and is each source ready? (no network)
python -m researcher_python sources
python -m researcher_python sources --format json

# Version, Python version and settings (API keys shown as set/unset only)
python -m researcher_python info

# Search, merged and deduplicated across sources
python -m researcher_python search "transformer attention" \
    --source openalex --source arxiv --max 10 --sort year

# One record, as a detail view or as a single JSON object
python -m researcher_python get 10.1103/PhysRevLett.116.061102
python -m researcher_python get 1706.03762 --format json

# Work-type policy: grants and standards are excluded by default. Datasets are
# not — they can be legitimate hits, and `prescreen` flags them later.
python -m researcher_python search "AI governance" --type article,review
python -m researcher_python search "AI governance" --exclude-type dataset
python -m researcher_python search "funding instruments" --type grant

# Fetch PDFs for one or more identifiers
python -m researcher_python download 1706.03762 10.1103/PhysRevLett.116.061102 \
    --output-dir papers
```

### Data quality: types, counts and versions

Three post-merge passes make a merged set usable rather than merely long.

**Work types.** `--type` / `--types` is an allow-list, `--exclude-type` adds to
the default deny-list, and `--no-default-exclude` starts from nothing. Every
source spelling (`journal-article`, `Journal Article`, `JournalArticle`) is
canonicalised, so a filter means the same thing across sources. The defaults
are **grant** and **standard**; `dataset` is deliberately not excluded (it can
be a real hit, and `prescreen` already flags one visibly). An unknown type is
kept — there is no evidence it is out, the same rule as an unknown year. The
pipeline reports `dropped_types` and a per-type breakdown separately from the
relevance/citation floors, and `prisma` uses it so *identified → deduped*
reconciles without pretending a grant was a duplicate.

**Citation and abstract backfill.** When OpenAlex participates in a search, the
pipeline batch-resolves every DOI-bearing record (and arXiv-only records via
their `10.48550/arxiv.X` DOI) in chunks of 50. It fills only **missing**
`citation_count`, `reference_count`, `abstract` and `work_type` — a Crossref or
Semantic Scholar count is never overwritten — and records
`citation_count_source`. The reply carries an `enrichment` report with
before/after coverage. In the 1,675-record CW1 corpus this moved citation
coverage from ~69% to ~99% and abstract coverage from ~76% to ~83%, which is
what makes the matrix's `citations_per_year` column usable. `--no-enrich`
disables it and `--enrich` forces it even when OpenAlex was not searched; by
default it only runs for the sources you actually asked for, so `--source
pubmed` does not silently acquire an OpenAlex lookup.

**Preprint/published versions.** A record that has both a preprint and a journal
version is kept as **one** `Paper` with a `versions` list. Links are explicit:
a shared arXiv id, the arXiv DOI (`10.48550/arxiv.X`), an OpenAlex `locations`
entry, a Crossref `is-preprint-of`/`is-version-of` relation, or a Semantic
Scholar `externalIds.ArXiv`. The published version supplies the bibliographic
fields (DOI, venue, year, type); the preprint keeps the arXiv id and open PDF.
Reader labels and notes are never touched, and identifier lookups resolve
through `versions[]`/`merge_keys()` under both the old and new DOI. A
title+year match no longer merges a preprint with a published record unless an
explicit link exists — a visible duplicate is better than a silently lost
version.

### The coursework workflow

Search → screen → read → matrix → reference list. Each step is a subcommand,
and the state carries between them in a saved JSON file.

```bash
# 1. Search, narrowed and recorded in the result file's meta.
#    --top-ratio cuts the merged set to its top tier; --sort score ranks by
#    the local relevance score rather than each source's own ordering.
python -m researcher_python search "AI governance framework" \
    --from 2019 --to 2024 --field title --sort score --top-ratio 0.5 \
    --format json --out results.json

# 2. Pre-screen: flag the obvious exclusions by rule, then judge the middle.
python -m researcher_python prescreen results.json --year-from 2019
python -m researcher_python prescreen results.json --year-from 2019 --apply

# 3. Screen: set labels, one record or a whole batch.
python -m researcher_python screen results.json --set core
python -m researcher_python screen results.json --id 10.1/a \
    --set excluded --reason "outside the date window"
python -m researcher_python screen results.json --note "check the method section"
python -m researcher_python screen results.json --batch updates.json

# 4. Snowball the core set: only records not already in the workspace, ranked
#    by how many core papers link to them (references, citations or both).
python -m researcher_python snowball --workspace research-out/cw1 --direction both

# 5. The two matrices the Week 3 lab asks for, plus the screening counts.
#    --require-screening refuses to emit a matrix whose rows are all
#    'unreviewed' — a matrix IS the screening decisions.
python -m researcher_python matrix   --from-file results.json --out matrix.csv --require-screening
python -m researcher_python matrix   --from-file results.json --columns key,label,score,title,notes
python -m researcher_python synthesis --from-file results.json --dimension venue --format markdown
python -m researcher_python summary  --from-file results.json

# 6. PRISMA-style counts: identified -> deduped -> screened -> included,
#    derived from the workspace log and the labels.
python -m researcher_python prisma research-out/cw1 --format markdown

# 7. Read a paper: text, or page-anchored quotes for your notes.
python -m researcher_python extract downloads/ --quotes --terms "governance"

# 8. The reference list — from the file you screened, so it is reproducible.
python -m researcher_python bibliography --from-file results.json \
    --accessed "22 October 2026" --out references.txt
```

A Literature Matrix is defined by its triage decisions, so a matrix whose rows
are all `unreviewed` is not a literature matrix — it is a search dump with a
header row. The tools therefore say so: `matrix` and `synthesis` report
`screening.ready`, warn on stderr when a set is unscreened, and refuse outright
under `--require-screening` / `require_screening=True`.

### Batch screening and pre-screening

`screen` (the verb; `label` names the stored field and remains a parser alias)
accepts a batch so screening 1,000 records does not take 1,000 calls:

```json
[
  {"id": "10.1/a", "label": "core"},
  {"ids": ["10.1/b", "10.1/c"], "label": "excluded", "reason": "off topic"},
  {"id": "10.1/d", "note": "re-read the method section"}
]
```

`--batch FILE` accepts a JSON array or JSONL (`-` reads stdin). The batch is
validated before anything is written; `--lenient` applies the valid entries and
reports invalid ones. Unmatched ids are reported, never silently ignored.
`python -m researcher_python label ...` is the compatibility alias and behaves
exactly like `screen`.

`prescreen` evaluates a fixed, ordered rule set — no title, no abstract, a
work type in `grant`/`standard`/`dataset`, and a year outside `--year-from`/
`--year-to` — and reports candidates by default. An unknown year is kept, and a
record that already carries a human label is skipped. `--apply` writes those
records as `excluded` with a `pre-screen:` reason; it is opt-in because "no
abstract" is a false positive for grey literature.

`snowball` selects the records labelled `core` (or explicit `--id`s), walks
`references`, `citations` or both, drops records already in the workspace, and
ranks the rest by how many core papers link to them. `--no-persist` previews;
by default the candidates merge into the workspace and the expansion is
recorded as a `snowball` provenance row. `--min-links 2` keeps only records
corroborated by two or more core papers.

`prisma` joins the append-only search log (identification, per-source counts)
to the labels (screened/excluded/included) and reports the flow, with
`--format markdown` for an appendix. Gaps are stated in `warnings`: an imported
workspace with no log has no honest identification count.

### Subcommands

| Command | What it does |
|---|---|
| `sources` | registered sources, capabilities and whether each builds |
| `info` | version, Python version, settings (secrets as set/unset) |
| `search` | merged, deduped search; `--from`/`--to`/`--field` narrow it, `--sort score`/`--top-ratio`/`--min-score`/`--min-citations` rank and filter it, `--workspace DIR` persists it, `--search-log` writes a standalone log |
| `get` | one record by DOI, arXiv id, PMID or ISBN |
| `download` | fetch open-access PDFs |
| `references` | works a paper cites |
| `citations` | works that cite a paper |
| `bibliography` | reference list from ids, a query, or `--from-file` |
| `matrix` | the Literature Matrix (CSV or Markdown) |
| `synthesis` | the Synthesis Matrix (a dimension × screening) |
| `summary` | screening counts per triage label |
| `screen` | screen records: set labels / notes, one or a batch (`label` is an alias) |
| `label` | alias of `screen` (the stored field is still called `label`) |
| `prescreen` | flag obvious exclusions by rule; report by default, `--apply` writes them |
| `snowball` | expand a core set through references/citations, ranked by link count |
| `prisma` | PRISMA-style flow counts from the workspace log and labels |
| `workspace` | create / inspect / log / round / export / import a workspace |
| `web-search` | Stack Overflow / Reddit records |
| `extract` | PDF text, or page-anchored quotes |

```bash
python -m researcher_python references 1706.03762 --max 20 --order year
python -m researcher_python citations 10.1145/3290600 --order citations
python -m researcher_python web-search '"merge dicts"' --max 5 --sort votes
python -m researcher_python extract downloads/paper.pdf --pages 5-9
```

`search`, `references`, `citations`, `bibliography`, `matrix`, `synthesis`,
`summary`, `screen`, `prescreen`, `snowball`, `prisma` and `web-search` all
take `--format` (table/json/jsonl/markdown/harvard/bibtex/ris/csl where
applicable), and `--out FILE` where the command writes a document.

`search` accepts `--format {table,json,jsonl,harvard,bibtex,ris,csl}` and
`--out FILE` (combining `--out` with `table` is a usage error). `--log-level
{DEBUG,INFO,WARNING,ERROR}` and `--version` are global.

**Exit codes:** `0` success, `1` runtime failure (for example every source
failed, or no PDF could be fetched), `2` usage or configuration error.
**Streams:** results go to stdout; summaries, warnings and per-source errors
go to stderr, so `... > out.json` stays clean.

## Library

```python
import researcher_python as rp

result = rp.search("deep learning", sources=["openalex"], max_results=3)
for paper in result.papers:
    print(f"{paper.year}  {paper.title}")

rp.to_json(result.papers, "papers.json", meta={"query": "deep learning"})
print(f"{len(result.papers)} papers, {len(result.errors)} source errors")

if result.papers:
    saved = rp.download_pdf(result.papers[0], "downloads")
    print("saved to", saved.path, "via", saved.via)
```

`search` never raises because one source failed — inspect
`result.errors` (source name → error) and `result.by_source` (raw counts).
`rp.get(identifier)` is the one-record form and does raise if no source has
it. Round-trip with `rp.to_json` / `rp.from_json`, or `to_jsonl` / `from_jsonl`
for a stream of `Paper.to_dict()` objects.

To query a source directly, build it from the registry:
`rp.create_source("arxiv", rp.load_settings())`.

### Narrowing a search

`search(..., filters=SearchFilters(...))` — or `--from/--to/--field` on the CLI,
`year_from`/`year_to`/`field` on the MCP tool — bounds a search so it can be
stated once, applied by every source, and written down.

```python
import researcher_python as rp

result = rp.search(
    "AI governance",
    sources=["openalex"],
    filters=rp.SearchFilters(year_from=2019, year_to=2024, field="title"),
)
print(len(result.papers), "papers inside the window")
```

Each adapter pushes down whatever its own API supports (OpenAlex
`from_publication_date`, Crossref `from-pub-date`, PubMed `[dp]`, arXiv's own
query language) and filters the rest locally, so the contract is identical even
where the API is not. A record whose year is unknown is **kept**: dropping it
would hide exactly the grey literature you most need to see and judge.

### Relevance scoring — making a merged search rankable

Every source ranks by its own private score, and none of those are comparable
across sources: OpenAlex's `relevance_score` and Crossref's ordering are
integers on undocumented scales. The pipeline's round-robin merge therefore
interleaves *positions*, not quality — a source's tenth-best hit lands next to
another source's best one. That is why an unqualified search can return a
cardiology paper beside a paper on AI liability: each source genuinely
believes its hit matched *its own* query expansion, and nothing downstream ever
says otherwise. Raising `max_results` cannot fix this — it caps each source
*separately* and so cannot remove a source's weakest hits afterwards.

`researcher_python.relevance` adds the missing signal: a **local, cross-source
score** computed from the query text and the record alone.

```console
$ python -m researcher_python search "legal issues of AI and mitigation" \
      --sort score --top-ratio 0.5
dropped 8 record(s) below --top-ratio 0.5 (scored against: legal, issues, ...)
39 papers from 5 sources (failed: none)
```

`score = 0.75 × coverage + 0.25 × density + phrase_bonus`, clamped to
`[0.0, 1.0]`:

- **coverage** — the fraction of query terms the record mentions at all. This
  dominates, which is what separates "on topic" from "shares a word".
- **density** — how saturated the record is with those terms, saturating rather
  than growing without bound.
- **phrase_bonus** — a small bonus when the query appears as a phrase in the
  title, the cheapest strong signal of a genuine topical match.

Title matches weigh 3× an abstract match, keywords 1.5×, venue 0.5×. The score
is **pure** (same record + query → same float, so a saved file stays
reproducible) and **topical only** — citations and open access are deliberately
excluded, because folding them in would make "relevance" drift as citation
counts drift. Impact is the separate, explicit `sort="citations"`.

Every record carries the score as `Paper.relevance` (round-tripping through
`to_json`/`from_json`, and available as the matrix `score` column), and
`search_papers` reports the `terms` it scored against plus the number `dropped`.

**Two floors, and which one to use:**

| floor | meaning | when |
|---|---|---|
| `min_score` (0.0–1.0) | absolute | you know the bar, e.g. "must mention most terms" |
| `top_ratio` (0.0<R≤1.0) | keep records ≥ `R × the best score` | **usually better** — cuts a result set to its top tier |

`top_ratio` is scale-free. `min_score` does not scale with query length: on a
six-term query almost any on-subject paper matches two or three terms, so a
`min_score` of `0.25` removed **nothing** on a real LSEP search. They compose —
the absolute floor removes junk, the relative one cuts the tail.

> **Raise `top_ratio` with care.** On a real six-term query, `0.4` and `0.5` cut
> exactly the same eight records (39 → 31), and every one was noise — AGI
> philosophy, unrelated topics. `0.6` additionally dropped *The Malicious Use of
> AI*, which is squarely on topic. A relative floor cannot distinguish
> "marginally relevant" from "topically irrelevant", so an aggressive ratio
> trades real recall for a tidy list. `relevance.DEFAULT_TOP_RATIO` is `0.5`.
> Scores tend to cluster with a gap after the weak tail, so a **plateau** (two
> ratios cutting an identical set) is your signal that you found the natural
> break rather than cutting into signal.

```python
import researcher_python as rp

result = rp.search(
    "legal issues of artificial intelligence",
    sources=["openalex"],
    top_ratio=0.5,
)
print(len(result.papers), "kept;", result.dropped, "dropped")
print(result.terms)                       # ['legal', 'issues', 'artificial', ...]
for paper in rp.sort_papers(result.papers, "score"):
    print(f"{paper.relevance:.3f}  {paper.title[:60]}")

# Or score anything yourself — including a hand-built list, or a saved file.
rp.score_papers(result.papers, "legal issues of artificial intelligence")
kept, dropped = rp.filter_by_score(result.papers, min_score=0.4, top_ratio=0.6)
```

### Ergonomics and triage, from real use

These exist because they were the friction points of actually running a
multi-round literature search, not because they were tidy to add.

**Convenience keywords on `search()`.** `field=`, `year_from=`, `year_to=` and
`min_citations=` mirror the CLI flags and the MCP tool, so the library is not
the awkward face:

```python
import researcher_python as rp

# Before: only filters=SearchFilters(field="title", ...) worked.
result = rp.search("AI regulation", sources=["openalex"], field="title")
```

Passing these **as well as** an explicit `filters=` raises rather than letting
one silently win — silent precedence is how a search stops being reproducible.

**Citation identifiers are accepted directly.** `citations()` and `references()`
used to require a `Paper`, so the obvious call failed with *"a citation query
needs a Paper, got str"* — a message that says what is wrong but not what to
do. All three faces now agree:

```text
rp.citations("10.1038/s42256-019-0088-2")   # resolves the DOI, then walks forward
rp.references("1806.07358")                 # arXiv id, walks backwards
```

**An empty result explains itself.** A field-scoped search is an AND match, so
a long keyword set legitimately finds nothing and returns no error. That is
exactly lab01's "title-match is tightly controlled — maybe too restricted"
trap, so the pipeline says which one you hit:

```console
$ python -m researcher_python search "accountability transparency conformity \
      assessment post-market monitoring AI regulation" --field title
0 papers from 4 sources (failed: none)
hint: 0 kept: field='title' requires ALL 9 query terms in the title (AND match).
Try fewer terms, or field='any' to relax it — a restrictive match returns fewer
hits but more exact ones (see lab01's title-match vs relaxed-search trade-off).
```

The same hint covers a citation floor that emptied the set, and a relevance
floor that did. `PipelineResult.hint` carries it; the CLI prints it to stderr
and the MCP tool returns it.

**`min_citations` as a standing filter.** Dropping low-cited records is a crude
way to filter venue quality when a venue allow-list cannot anticipate every
field's good journals. An **unknown** citation count is kept, like an unknown
year under a date filter — never hide grey literature. Because raw counts
reward age, the Literature Matrix also carries `citations_per_year`:

```console
$ python -m researcher_python matrix --from-file core.json \
      --columns key,year,citations,citations_per_year,title
```

| year | citations | citations_per_year |
| --- | --- | --- |
| 2019 | 6120 | 765.00 |
| 2018 | 3930 | 436.67 |
| 2023 | 100 | 25.00 |

That last row is the point: 100 citations in a recent paper can outrank 400 in
a 1992 one, and the raw count cannot see it (lab03's caution about citation
counts and age).

**`--search-log FILE`** appends one provenance row per search — query, field
scope, sources, kept, dropped and the terms scored against — to a Markdown
table, creating the header if the file is new:

```console
$ python -m researcher_python search "algorithmic bias" --field title \
      --search-log research-out/rounds.md
$ cat research-out/rounds.md
| when | query | field | sources | round | kept | new | seen | screened | dropped | errors | terms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-20T13:55:00Z | algorithmic bias | title | openalex, ... | 1 | 16 | 16 | 0 | 0 | 0 | | algorithmic; bias |
```

The module's lab is explicit that the *combination* which produced a result
must be recorded, not just the query — otherwise a multi-round session cannot
be reviewed. Header and rows are rendered from the same column definition, so
they cannot drift apart. `just search-log FILE "query" --field title` is the
shorthand.

### Workspaces (state + provenance)

A workspace is a directory that owns one result set, its append-only search log
and its meta. Every search — CLI or MCP — writes through the same
`Workspace.record_search` path:

```
<workspace>/
  workspace.json       # round, counts, provenance, last search
  results.json         # the canonical merged set, with a meta snapshot
  search-log.jsonl     # machine log: all params, by_source, errors
  search-log.md        # human table, regenerated from the JSONL
  rounds/round-N/      # snapshots taken by `workspace round --next`
```

```console
$ python -m researcher_python workspace init research-out/cw1 --topic "AI governance"
$ python -m researcher_python search "code of ethics" --field title \
      --workspace research-out/cw1
workspace: 12 new, 27 already seen (8 screened), 412 total
$ python -m researcher_python search "professional responsibility" \
      --workspace research-out/cw1
workspace: 4 new, 38 already seen (8 screened), 416 total
$ python -m researcher_python workspace status research-out/cw1
$ python -m researcher_python workspace round research-out/cw1 --next
$ python -m researcher_python matrix --workspace research-out/cw1 --out matrix.csv
$ python -m researcher_python bibliography --workspace research-out/cw1 --out refs.txt
```

Searching into an existing workspace dedupes against prior rounds using the
same id/title+year rules as a single search, and reports `N new, M already
seen (K screened)`. A record already labelled `core`/`general`/`excluded` keeps
that label and its notes; an excluded record is never resurrected. A relative
workspace path resolves under `RESEARCHER_HOME` when set, so a server launched
from another directory (an Obsidian vault, say) writes where you expect.

`workspace import` adopts pre-workspace artefacts without rewriting them. It
records the reconciliation honestly: `logged_kept`, `unaccounted` (records in
the result set with no search row), `not_in_result` (rows that did not survive
dedupe/filters) and `no_log_records` (a file imported with no log at all). It
never invents a provenance row.

`--mode snapshot` (the default) treats the file as the whole set;
`--mode merge` folds it into an existing workspace, dedupes, and reports
`new`/`seen`/`total`. Use `merge` for a set of disjoint legacy round files.

```console
$ python -m researcher_python workspace import research-out/cw1 \
      --results cw1-round3-screened-1077.json \
      --log cw1-round3-search-log.md --round 3 --mode merge --dry-run
```

### Citations

`references(paper)` and `citations(paper)` fan a citation query across the
sources that declare the matching `Capabilities` flag (OpenAlex and Semantic
Scholar for both directions, Crossref for references, PubMed for both) and
merge the answers into one `CitationResult`: `.papers` (deduped, canonically
ordered), `.edges` (each record mapped to the sources that asserted it), plus
`.errors`, `.by_source` and `.truncated`, so a missing or capped source is
visible rather than silently shrinking the answer. `order=` accepts `canonical`
(the default), `year`, `citations` or `title`; `limit=` caps each source;
`strict=True` raises if any requested source fails, which is what makes a
returned result reproducible.

Determinism here means the **local** transformation: given the same per-source
responses, the result is a pure function of that input — source order, caller
order and arrival order cannot change it. It is deliberately *not* a claim that
a live query is byte-reproducible over time, since the citation graph itself
changes. CLI subcommands for citations are not wired up yet.

### Harvard referencing

`render_harvard` / `to_harvard` produce a **Harvard (Cite Them Right)**
reference list — the style GCU's coursework briefs mandate — and
`harvard_citation` produces the matching in-text form. Both are local and pure,
like the other formats.

```console
$ python -m researcher_python search "malicious use of AI" --source arxiv \
      --max 2 --format harvard --accessed "22 October 2026"
Brundage, K. et al. (2018) The Malicious Use of AI: Forecasts, Prevention, and
Mitigation. arXiv. Available at: https://arxiv.org/abs/1806.07358 (Accessed: 22
October 2026).

Cairnstew, D. and Tianfield, H. (2020) Malicious Uses and Abuses of AI. Report
```

```python
import researcher_python as rp

papers = rp.search("AI governance", sources=["openalex"]).papers

# In-text form for a draft that holds identifiers, not names.
print(rp.harvard_citation_map(papers))
# {'10.1000/x': '(Smith et al., 2020)', '10.1000/y': '(Jones and Bo, 2021)'}
```

The list is ordered alphabetically by first-author surname — the one thing a
BibTeX dump cannot give you. `harvard_citation_map(papers)` returns
`{stable id: "(Author, Year)"}` so a draft can hold identifiers and resolve the
human form later, without re-searching.

Two honest limits, both visible rather than papered over:

- Cite Them Right wants an edition and a place of publication for books and
  reports. `Paper` carries neither, so those segments are **omitted**, not
  invented — fill them in from the book itself.
- `accessed=` is an explicit argument (never read from a clock), so a citation
  without it is byte-stable.

Corporate authors (`ACM`, `OECD`, `United Nations`) are cited by their full
name rather than being split into initials — set
`Author(name="OECD", organization=True)` to get that; without the flag,
"United Nations" would render as the implausible "Nations, U.".

### Literature and synthesis matrices

`matrix.py` renders the Week 3 lab's two artefacts as **tables**, not
citations: a **Literature Matrix** (one row per paper) and a **Synthesis
Matrix** (a dimension cross-tabbed against screening). Both go out as CSV
(spreadsheet / `pandas`) or Markdown (paste-ready).

```python
import researcher_python as rp

papers = rp.search("AI governance", sources=["openalex"]).papers

# Every available column name, so a caller can pick without reading the source.
print(rp.column_names())
# ['key', 'label', 'authors', 'year', 'title', ... 'exclusion_reason', 'notes']

rp.to_matrix_csv(papers, "matrix.csv", columns=("key", "label", "year", "title"))
rp.to_synthesis_markdown(papers, "synthesis.md", dimension="venue")
print(rp.screen_summary(papers))
# {'unreviewed': 2, 'core': 0, 'general': 0, 'excluded': 0, 'total': 2}
```

```console
$ python -m researcher_python synthesis --from-file results.json --dimension venue
venue,unreviewed,core,general,excluded,total
Journal of Governance,1,4,3,0,8
A conference,1,1,2,2,6
```

Columns are named extractors over `Paper` (`rp.column_names()` lists them all),
so adding one needs no change to the module. `score` is available but *not* a
default column — most records in a hand-built or `get`-sourced set were never
scored, and a column empty for every row is noise — so opt into it with
`--columns key,label,score,title,notes` when the set came from a scored search.

### Screening is not optional

`matrix.screening_report(papers)` answers "is this a *screened* set, or just a
raw search dump?", and the renderers can act on it:

```console
$ python -m researcher_python matrix --from-file results.json
key,label,authors,year,title,venue,type,doi,open_access,abstract,notes
10.1/a,unreviewed,...                      <- rendered, but flagged below
note: screening incomplete: 2 of 2 records are still 'unreviewed'. A matrix
records screening decisions, so label them first: researcher-python label FILE
--set core|general|excluded

$ python -m researcher_python matrix --from-file results.json --require-screening
screening incomplete: 2 of 2 records are still 'unreviewed'. ...
$ echo $?
1
```

```python
import researcher_python as rp

papers = [
    rp.Paper(doi="10.1/a", title="AI governance in practice").label_as("core"),
    rp.Paper(doi="10.1/b", title="Regulation of AI in the EU").label_as("general"),
    rp.Paper(doi="10.1/c", title="Cardiovascular burden").label_as("excluded", "off topic"),
]
print(rp.screening_report(papers))
# {'unreviewed': 0, 'core': 1, 'general': 1, 'excluded': 1, 'total': 3,
#  'screened': 3, 'ready': True, 'message': ''}

# Refuse to emit one at all, instead of warning after the fact:
rp.to_matrix_csv(papers, "matrix.csv", require_screening=True)
```

An empty set is never `ready` — "no records" is not a screened set, so this
catches a query that silently returned nothing too. An `excluded` record *does*
count as screened: an exclusion is a decision.

### Screening state

`Paper` carries three reader-owned fields that no source ever populates:

```python
import researcher_python as rp

# (From a search here so the example also runs offline against a
# single fake source, the way the docs test does.)
paper = rp.search("screening", sources=["openalex"]).papers[0]

paper.label_as("core")                       # unreviewed|core|general|excluded
paper.label_as("excluded", "outside window") # reason kept only for `excluded`
paper.note("check the method section")       # dated, appended

rp.to_json([paper], "screened.json")         # the labels travel with the file
print(rp.screen_summary([paper]))
# {'unreviewed': 0, 'core': 0, 'general': 0, 'excluded': 1, 'total': 1}
```

They survive `to_json`/`from_json`, so a screening session is resumable and a
later matrix, synthesis or bibliography render reflects the decisions. Merging
follows the usual "first seen wins, fill the gaps" rule, so a label is never
clobbered by a fresher source hit. An unknown label raises rather than storing
junk; `Paper.triage_label()` reads an unrecognised value as `unreviewed`.

### The screening loop as a library

The three pure pieces behind the CLI/MCP screening commands are public:

```python
import researcher_python as rp

papers = [
    rp.Paper(doi="10.1/a", title="AI governance", abstract="text", year=2022),
    rp.Paper(doi="10.1/b", title="A dataset", abstract="text", year=2022,
             work_type="dataset"),
    rp.Paper(doi="10.1/c", title="Old work", abstract="", year=2010),
]

# 1. Batch labels: validate, then apply in one pass.
outcome = rp.apply_updates(
    papers,
    [
        {"id": "10.1/a", "label": "core"},
        {"ids": ["10.1/b", "10.1/c"], "label": "excluded", "reason": "screened out"},
    ],
)
print(outcome.labelled, outcome.unmatched)

# 2. Pre-screen candidates (a report, not a decision).
report = rp.prescreen(papers, rp.PrescreenCriteria(year_from=2019))
print(report.flagged, report.counts_by_rule)

# 3. PRISMA-style flow, from labels + an (here empty) log.
print(rp.prisma_report(papers, []))
```

`rp.snowball_expand(core_papers, direction="both", known=papers)` is the
network half of snowballing: it calls `citation.references`/`citations`, drops
records in `known`, and returns `SnowballResult` candidates ranked by
`Candidate.links`. `Workspace.expand` wraps it to select the `core` label,
persist the candidates and write the `snowball` provenance row.

### Bibliography formats

`to_bibtex`, `to_ris` and `to_csl_json` render any paper list **locally** — no
network, no service — into the three formats a toolchain actually ingests:
BibTeX entries, RIS records, and canonical CSL-JSON items (standard
`{"family", "given"}` author shape and `issued.date-parts`, so any citeproc
implementation can style them — no bespoke mapping). Each is a pure function of
the records: the same list in, the same bytes out, in the given order. Writers
accept a path or an open file object and write atomically, like `export`.
`render_bibtex` / `render_ris` / `render_csl_json` return the text as `str`;
`bibtex_key` builds a deterministic, ASCII citation key.

## Web sources (experimental)

Beyond the scholarly pipeline, the package can pull *structured web content* —
Q&A sites, forums, issue trackers — through a separate, modular framework.
Web sources answer "what content on this site matches?", not "which paper is
this?", so they have their own record model (`WebResult`, JSON-safe, with a
source-specific long tail kept in `extra`) and their own registry
(`rp.web_source_names()`), deliberately apart from `Paper` and
`DEFAULT_SOURCES`.

| name | model | site | status |
|---|---|---|---|
| `stackoverflow` | `WebResult` | api.stackexchange.com | offline-tested + live demoed |
| `reddit` | `WebResult` | oauth.reddit.com | offline-tested (live pending OAuth app) |

```console
$ just web-search '"merge dicts"' --max 5 --sort votes
score       date      tags                       title  url
  165  2011-05-10  #python #dictionary #merge    How to merge dicts, collecting values from matching keys?  ...
    7  2016-10-12  #python #dictionary #python-3.5  How to 'deep-merge' dicts?  ...
```

```console
import researcher_python as rp

# One structured call: retries RateLimitError settings.retries times (D8).
result = rp.web_search("merge dicts", max_results=5, sort="votes")
print(result.results[0].title, result.results[0].score)
rp.to_json([r.to_dict() for r in result.results], "so.json")

# Any Stack Exchange site, and the adapter directly:
source = rp.create_web_source("stackoverflow", site="serverfault")
print(source.search("vim", max_results=3).quota)   # {'max': 300, 'remaining': ...}
```

`web_search` fans out to *one* source today; a multi-site merge (Stack Overflow
+ Super User) is the same shape as the scholarly pipeline and can be added on
top of the `WEB_SOURCES` registry later. Stack Exchange API behaviour is
honoured at the adapter: the `site=` parameter, the response wrapper's
`has_more` / `quota_*` / `backoff` (backoff feeds the throttle for the next
request), and `error_name`-based error mapping (`throttle_violation` →
`RateLimitError`, everything else → `SourceError`). An optional
`RESEARCHER_STACKEXCHANGE_KEY` lifts the anonymous 300/day IP quota.

Reddit behaves differently where its API differs: Reddit closed anonymous
JSON access (any keyless `/search.json` answers HTTP 403), so the `reddit`
adapter authenticates with OAuth 2.0 *client credentials* from
`RESEARCHER_REDDIT_CLIENT_ID` / `RESEARCHER_REDDIT_CLIENT_SECRET` (a free
registered app) and calls `oauth.reddit.com`. It pages the Listing protocol
(`after` cursor + `count` instead of page numbers), reads rate hints from the
`X-Ratelimit-*` response headers into the same throttle the SE `backoff`
feeds, honours `sort` ∈ `relevance|hot|top|new|comments` and `t=` time
windows, and scopes with `subreddit=`:

```console
$ just web-search '"merge dicts"' --source reddit --subreddit python --time month --max 3
config: setting 'reddit_client_id' is required; export RESEARCHER_REDDIT_CLIENT_ID first
```

```console
# Without credentials this fails cleanly as a ConfigurationError, before any
# request; with them it returns live records (pinned offline in the tests).
source = rp.create_web_source("reddit", subreddit="learnpython", time_range="month")
print(source.search("merge dicts", max_results=3).results[0].title)
```

Adding another web source is the same four steps every time: subclass
`WebSource`, implement `query_params` + `parse_payload`, add one
`WEB_SOURCES` entry, and pin the parser with an offline fixture. See
`researcher_python/web_sources/base.py` for the contract.

### Citing web records

Web results are citable: `render_web_bibtex` / `render_web_ris` /
`render_web_csl_json` (plus the `web_to_bibtex` / `web_to_ris` /
`web_to_csl_json` writers) turn a `WebResult` list into BibTeX `@misc`
entries, RIS `GEN` records, and CSL `webpage` items — rendered locally and
deterministically, exactly like `format.py` does for papers, with
web-specific conventions: usernames stay CSL `{"literal": ...}` (never a
fake family/given split), the epoch `created` date becomes UTC `date-parts`,
the site maps to a display name for `container-title` / `howpublished` /
`T2`, and the CSL id is `<source>-<id>` (e.g. `stackoverflow-5946236`).
`accessed=` is an explicit argument (BibTeX `note`, RIS `N1`, CSL
`accessed`) so a citation *without* it is byte-stable.

```console
$ just web-search '"merge dicts"' --max 2 --format bibtex --out so.bib
wrote so.bib (2 records)
$ cat so.bib
@misc{salil2011merge,
  author = {Salil},
  title = {How to merge dicts, collecting values from matching keys?},
  howpublished = {Stack Overflow},
  year = {2011},
  url = {https://stackoverflow.com/questions/5946236/...}
}
```

## MCP server

The same pipeline is exposed as [Model Context Protocol](https://modelcontextprotocol.io)
tools, so an MCP client (opencode, Claude Desktop, …) can search, read, cite and
download papers without shelling out to the CLI. It is a thin third face on the
public API — a tool *is* a library call, with an MCP signature — so nothing is
re-implemented and behaviour matches the CLI exactly.

Install the optional dependency and run the server over **stdio** (the standard
MCP transport; the client spawns it as a subprocess):

```bash
pip install 'researcher-python[mcp]'   # or add `mcp` to the Nix shell/flake
researcher-python-mcp
```

The MCP SDK is an **optional extra**, so `import researcher_python` and the
CLI stay lightweight and work without it.

### Tools

| Tool | What it does | Wraps |
|---|---|---|
| `sources` | every scholarly + web source and its capabilities | registry |
| `info` | version and effective settings (secrets as set/unset) | `config` |
| `open_workspace` | make a directory this session's persistence target | `Workspace` |
| `workspace_status` | counts, round, provenance and paths | `Workspace.status` |
| `close_workspace` | stop persisting (clear the session default) | `Workspace` |
| `workspace_next_round` | snapshot and advance the round | `Workspace.start_round` |
| `search_papers` | merged, deduped search; persists when a workspace is open | `pipeline.search` |
| `get_paper` | one record by DOI / arXiv id / PMID | `pipeline.get` |
| `references` | works the given paper cites | `citation.references` |
| `citations` | works that cite the given paper | `citation.citations` |
| `download_pdf` | fetch the open-access PDF | `download.download_pdf` |
| `bibliography` | render BibTeX / RIS / CSL-JSON | `format.render_*` |
| `web_search` | Stack Overflow / Reddit records | `web_sources.web_search` |
| `matrix` | Literature Matrix (CSV/Markdown) | `matrix.render_matrix_*` |
| `synthesis` | Synthesis Matrix + screening counts | `matrix.render_synthesis_*` |
| `screen_summary` | counts per triage label | `matrix.screen_summary` |
| `label` | set labels / notes on a saved result set or workspace | `Paper.label_as` / `.note` |
| `screen` | verb alias of `label`; accepts a batch of updates | `screening.apply_updates` |
| `prescreen` | flag obvious exclusions by rule (report by default) | `screening.prescreen` |
| `snowball` | expand the core set through the citation graph | `snowball.expand` |
| `prisma` | identified/deduped/screened/included flow | `prisma.prisma_report` |
| `extract_text` | plain text of a local PDF | `extract.extract_text` |
| `find_quotes` | page-anchored quotes from a PDF | `extract.find_quotes` |

Example client configuration (spawns the console script over stdio):

```json
{
  "mcpServers": {
    "researcher": {
      "command": "researcher-python-mcp",
      "env": { "RESEARCHER_EMAIL": "you@example.com" }
    }
  }
}
```

**Wired up in this repo already.** `.opencode/opencode.json` (committed)
registers the server for opencode, so the tools are available in this repo
without any manual setup — no `nix build` required, it launches through the
flake dev shell:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "researcher": {
      "type": "local",
      "command": ["nix", "develop", "--command", "python", "-m", "researcher_python.mcp_server"],
      "cwd": ".",
      "enabled": true,
      "timeout": 120000 // a cold `nix develop` outlives the 5s default
    }
  }
}
```

opencode exposes them as `researcher_<tool>`. The two repo skills
(`.opencode/skills/research-rounds`, `.opencode/skills/paper-analysis`) drive
the server through these tools rather than the CLI; the CLI stays available as
a fallback. `tests/test_docs.py` guards the config and both skills — the
configured argv must name a real entry point, every `researcher_*` tool a skill
mentions must exist, and the config must stay git-trackable.

**Global registration (module, not applied).** The repo-local entry above has
`"cwd": "."`, so it only exists when opencode is opened in this checkout. For
work in an Obsidian vault, import the flake's module and enable it on the host:

```nix
# nixos-config flake.nix: add inputs.researcher-python
imports = [ inputs.researcher-python.homeManagerModules.default ];

my.programs.researcher-python = {
  enable = true;
  package = inputs.researcher-python.packages.${pkgs.system}.default;
  home = "${config.home.homeDirectory}/.local/share/researcher-python";
  envFile = "${config.home.homeDirectory}/.config/researcher-python/.env";
};
```

`nix/opencode-module.nix` registers `my.programs.opencode.mcp.researcher` with
the absolute store path and `RESEARCHER_HOME` / `RESEARCHER_ENV_FILE`, so the
server starts from any directory. It is **not** applied by this flake — a host
must opt in with `enable = true`. Do not point `envFile` into the Nix store;
keep credentials in your home directory or agenix.

> **Do not print to stdout in the dev shell.** A stdio MCP server speaks
> JSON-RPC on stdout; the flake's `shellHook` therefore writes its banner to
> **stderr**, and any new `shellHook` output must do the same.

Every tool returns a JSON object with `"ok": true` and the payload, or
`{"ok": false, "error": "<ClassName>", "message": "…"}` on failure — a
`ResearcherError` (missing credential, unknown source, no PDF, …) becomes
actionable data rather than a crashed server, and `ConfigurationError` keeps
its message so the model can name the env var to set. Per-source failures in
`search`/`references`/`citations` are reported in an `errors` object and never
abort the call. Tools read `.env`/`RESEARCHER_*` via `load_settings()`, so
configuration is honoured exactly as in the CLI.

`search_papers` also takes `sort="score"`, the `min_score`/`top_ratio`
floors and `min_citations`, and returns each record's `relevance` plus the
`terms` scored against and the number `dropped`, with a `hint` when the set is
empty for a guessable reason.

**Workspace persistence.** Call `open_workspace(path)` once (or pass
`workspace="path"` on a call) and `search_papers` persists automatically: it
merges into `results.json`, appends the log entry and returns
`persisted: true` with `counts.new` / `counts.seen` / `counts.screened` /
`counts.total` plus a `meta` block containing the params, `by_source` and
errors. Without a workspace the tool stays pure and returns
`persisted: false`. `matrix`, `synthesis`, `screen_summary`, `bibliography` and
`label` accept `workspace=` as an alternative to `from_file=`. Pass **`compact=True`** for a triage-sized
projection (identity, standing, relevance, a 240-character abstract excerpt)
instead of the full record — measured at ~74% smaller, which is the difference
between fitting an agent's context and not. `matrix`, `synthesis` and `screen_summary` return a
`screening` report (`ready` / `message`) and accept
`require_screening=True` to refuse an unscreened set. The server's
**instructions** state both, so an MCP client is told to prefer a scored search
with a floor over a bigger `max_results`, and that screening — not searching —
is what turns a result set into a Literature Matrix.

## Errors

Every exception derives from `ResearcherError`:

| Class | Raised when |
|---|---|
| `ConfigurationError` | a required setting is missing, or an unknown source/sort was requested → exit 2 |
| `SourceUnavailableError` | the adapter cannot be built (missing dependency or credential) |
| `SourceError` | transport or API failure | 
| `NotFoundError` | the source has no such record |
| `RateLimitError` | the source answered 429 (carries `retry_after`) |
| `DownloadError` / `NoPdfError` | a PDF could not be fetched / no PDF URL exists |
| `ExportError` | a result set could not be written or read back |
| `UnsupportedOperation` | the source does not implement that capability |

The CLI prints each as a single line on stderr (a traceback only at
`--log-level DEBUG`) and maps `ConfigurationError` to exit 2, everything else
to exit 1.

## Live verification

The offline suite never touches the network, so it can only prove that the
adapters parse and refuse correctly. To confirm them against the real APIs,
run the manual probe from an address that is not already rate-limited:

```bash
nix develop --command python scripts/verify_live.py
nix develop --command python scripts/verify_live.py --source crossref --source semantic_scholar
nix develop --command python scripts/verify_live.py --dry-run
```

Each source is exercised through every capability its `Capabilities` declares
— construct, search, get, wrong-identifier, references, citations, pdf-url,
plus a DOI case-sensitivity check for Crossref — printed as one row per check
with a polite delay between calls (`--delay`, default 2s). Outcomes are
`PASS`, `FAIL`, `SKIP` (unsupported or unconfigured) and `RATELIMIT`; after a
429 the remaining checks for that source are skipped rather than hammered.
`--format json` emits the same rows as a list.

Exit codes: `0` clean, `1` any `FAIL`, `3` any `RATELIMIT`, `2` usage or
config error. The identifiers it probes live in `KNOWN_IDS` at the top of the
script, so they are easy to edit when an API changes.

## Extending

Every new runtime dependency lands in three places — `pyproject.toml`,
`nix/default.nix`, and the `flake.nix` dev shell (`nix develop` re-reads all
three). A new source is a module under `researcher_python/sources/` plus one
entry in that package's `_REGISTRY`; `python -m researcher_python sources`
then reports whether it builds and which capabilities it declares. A new MCP
tool is a `_tool_*` function plus one `@server.tool()` registration in
`researcher_python/mcp_server.py`, with its name added to `TOOLS`.

## Known limitations

- **Relevance scoring is lexical, not semantic.** It matches the query terms you
  typed against title/abstract/keywords/categories/venue. It cannot tell that a
  paper is on topic while using entirely different words ("governance" vs
  "oversight"), so a synonym-rich or badly-worded query still needs the
  `min_score`/`top_ratio` floors checked by eye, or a `field`/`query` rephrase.
- **An aggressive `top_ratio` costs recall.** See the warning above: `0.6`
  removed a squarely on-topic paper in testing. Prefer `0.5`, and treat a
  plateau in the `dropped` count as the signal to stop.
- **Not a substitute for the reading packs.** Policy trackers (IAPP, Norton
  Rose), legislation databases and paywalled textbooks still need manual
  lookup; `openaire` covers repositories and theses, not statutory sources.
- **Harvard has no edition/place data.** Omitted, not invented — see above.
- **Screening state is per-file or per-workspace, not a cross-file library.**
  Two standalone result files for the same topic hold independent labels; use
  a workspace when you want one shared set, log and PRISMA report.
- `Bio.Entrez` stores its `email`/`tool`/`api_key` **process-globally**, so two
  `PubMedSource` instances configured differently in one process clobber each
  other.
- Semantic Scholar effectively needs an API key; keyless requests are
  frequently HTTP 429 from shared IPs.
- `crossref` and `semantic_scholar` are implemented and fixture-tested but not
  yet verified against the live APIs (deferred to `scripts/verify_live.py`).
- No PDF is downloaded for a record with no open-access copy: `pdf_url` is
  empty, Unpaywall has no location, and `NoPdfError` is raised.
