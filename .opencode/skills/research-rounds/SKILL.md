---
name: research-rounds
description: >-
  Use when the user wants to research a topic through iterative keyword-refinement
  rounds — deriving keywords, searching, reading, writing notes, and refining
  until the literature saturates. The loop is self-contained here; searches run
  through this repo's researcher MCP server. Triggers: "research this topic for
  me", "run research rounds", "keyword refinement rounds", "literature search
  loop", "Re-Search".
---

# Research Rounds (the multi-round keyword loop, run properly)

A skill for doing literature research through iterative rounds:
**Research = Re-Search** — understanding is built over multiple rounds, not in
one pass. The loop, its rules and its failure modes are embedded here, so a
session needs no external document.

## Tooling — drive the researcher MCP server

The `researcher` MCP server (declared in `.opencode/opencode.json`, served by
this repo's `researcher_python.mcp_server`) **is** the Search/Download half of
the loop. Prefer it over ad-hoc web searching, so every round is reproducible
and citable. opencode exposes its tools as `researcher_<tool>`:

| Tool | Use it for |
|---|---|
| `researcher_search_papers` | fan a query across all 5 sources, merged + deduped |
| `researcher_prescreen` | flag obvious exclusions by rule (no abstract, grant/standard/dataset, outside the year window) — reports by default |
| `researcher_screen` | set `core`/`general`/`excluded` + notes, one record or a batch (`updates=[...]`) |
| `researcher_snowball` | expand the `core` set through references/citations; only new records, ranked by link count |
| `researcher_report` | `kind="prisma"` for identified → deduped → screened → included counts; `kind="matrix"`/`"synthesis"` for the artefacts |
| `researcher_get_paper` | one record by DOI / arXiv id / PMID |
| `researcher_references` | works a paper cites — the prior art behind it |
| `researcher_citations` | works citing a paper — how the field built on it |
| `researcher_download_pdf` | fetch the open-access PDF (Unpaywall fallback) |
| `researcher_bibliography` | render BibTeX / RIS / CSL-JSON for a result set |
| `researcher_web_search` | Stack Overflow / Reddit (practical + community angles) |
| `researcher_sources` | which sources exist and what each supports |
| `researcher_info` | what is configured (keys shown set/unset only) |
| `researcher_open_workspace` | make a directory the session's result set + log |
| `researcher_workspace_status` | round, counts and provenance for the audit trail |
| `researcher_workspace_next_round` | snapshot the round and start the next |
| `researcher_close_workspace` | stop persisting (clear the session default) |

Credentials come from the repo's `.env` (`RESEARCHER_EMAIL` at minimum; an
OpenAlex and/or Semantic Scholar key is strongly recommended so sources do not
throttle). Call `researcher_info` once at the start of a session if you are
unsure what is configured — it never leaks a key value.

**Reading the results.** Every tool returns `{"ok": true, ...}` or
`{"ok": false, "error": "<ClassName>", "message": "..."}`. On `ok: false` the
message names what to fix (usually a missing env var) — report it and continue
with the sources that did answer rather than abandoning the round.

- `search_papers` returns a **summary page** by default (`limit=10`): read
  `summary`, `kept`, `has_more` and `next_offset`, then pass `offset` for the
  next page. It also returns `by_source` (raw per-source counts) and `errors`
  (per-source failures). A throttled source is **normal**: it appears in
  `errors` while the others still return records. `max_results` is the
  per-source retrieval cap, not the page size.
- **Prefer `sort="score"` with `top_ratio` over raising `max_results`.** Each
  source ranks by a private, mutually incomparable score, so a merged set
  otherwise keeps every source's weakest hits; `max_results` caps each source
  *separately* and cannot remove them afterwards. Pass `top_ratio=0.5` to keep
  the top tier (scale-free, so it works on long queries, where a `min_score`
  removes nothing). The reply carries each record's `relevance`, the `terms` it
  was scored against, and `dropped` — report the drop count rather than
  presenting a filtered list as if it were everything. Do not push `top_ratio`
  past ~0.6: it starts discarding genuinely on-topic work.
- `references` / `citations` take an **identifier** (DOI/arXiv id/PMID), not a
  title — resolve a title with `search_papers` first. Both accept `limit`,
  `order` (`canonical`/`year`/`citations`/`title`) and `sources`.
- **A field-scoped search is an AND match.** `field="title"` requires *every*
  query term in the title, so a long keyword set returns nothing and no error.
  If a reply comes back empty, read its `hint` — it names the cause (field AND
  match, citation floor, or relevance floor) and the relaxation to try. This is
  lab01's title-match-vs-relaxed trade-off: restrictive gives fewer, more exact
  hits; relaxed gives more and a heavier manual scan.
- **Open a workspace before the first search.** Call
  `researcher_open_workspace` with an absolute path (or a name under
  `RESEARCHER_HOME`) once. Then every `researcher_search_papers` persists
  automatically: records merge into `results.json`, one append-only log row is
  written with all params + `by_source` + `errors`, and the reply carries
  `counts.new` / `counts.seen` / `counts.screened` / `counts.total`. Report
  those counts — `12 new, 27 already seen (8 screened)` is the round-3-to-4
  answer this skill is for. Call `researcher_workspace_next_round` between
  rounds (it snapshots first), and `researcher_workspace_status` when you need
  the audit trail. Do not hand-write a round log; `--search-log` remains for a
  standalone file, and the CLI rejects it together with `--workspace` because
  there is only one write path.
- **Screen in batches, then snowball.** A round's real value is the screening,
  not the search dump. Run `researcher_prescreen` first (report-only) to see
  the obvious exclusions, then `researcher_screen` with `updates=[{id, label,
  reason, note}, ...]` to record the middle in one call. Once a few records are
  `core`, `researcher_snowball` walks their references/citations and returns
  only records not already in the workspace, ranked by how many core papers
  link to them — a stronger recall engine than another keyword query. Finish a
  round with `researcher_report(kind="prisma")` for the identified → deduped →
  screened → included counts the write-up needs.
- **Work from the summary page.** `search_papers` returns identity, standing,
  relevance and label by default — no abstracts — and pages at 10 rows. Use
  `detail="compact"` for a 240-char excerpt, `detail="full"` or `get_paper` for
  the complete record of one paper, and `fields=["doi","title","year"]` to
  project exactly what you need. Paging a workspace search adds no new log row;
  `page_source` says whether the page came from the cached search or the
  persisted set.
- **Standing, not just relevance.** `min_citations` filters on raw citations,
  which reward age; `citations_per_year` (a matrix column) is the age-normalised
  view and is the better triage signal. An unknown citation count is always
  kept — do not read its absence as a low score.
- `bibliography` takes either `identifiers: [...]` or `query: "..."` plus
  `format` (`bibtex`/`ris`/`csl_json`) and returns the rendered text — use it
  to hand back a citable reference list for a round without writing files.

**CLI fallback** (when the MCP server is unavailable, or you want files on
disk): `just research '"phrase"' --out research-out/round-1`, `just cli --
search --workspace research-out/round-1`, `just ws-status research-out/cw1`,
`just web-search`. The quoting trap still applies there — a multi-word query
needs nested quotes and each `--source` needs its own flag. A workspace owns
`results.json`, `search-log.jsonl`, `search-log.md` and `workspace.json`; prefer
it over scattering `papers.json`/`rounds.md` files by hand.

## The loop

```
Round One:  Idea/Topic ──Derive──> Keywords ──> { Search → Download → Read → Write }
                                                          │
                                                    Update your understanding
                                                          │
                                                    Updated Knowledge
New Round:  Updated Knowledge ──Refine──> Refined Keywords ──> { Re-Do: Search → Download → Read → Write }
                                                          │
                                                    Further update your understanding
                                                          │
                                                          └──> … New Round
```

Run rounds until **saturation**: a round that surfaces no materially new
sources, terms, or changes to your understanding. Say explicitly when
saturation is reached and why.

## Division of labour

The loop needs three things: search, reading, and judgment. Split them so
neither side becomes the bottleneck.

- **The human owns**: the idea/topic, the constraints, the judgment of whether
  a source actually supports a claim, and the final argument and voice.
- **You (the assistant) own the mechanics**: derive candidate keywords, run the
  searches, download the open-access PDFs, extract the text, read the sources,
  and write the per-source notes and the matrix. Reading is part of your job —
  the tooling exists (`download_pdf`, `extract_text`, `pdf-mcp`) precisely so a
  round does not end with "now you read these".
- **The human audits, rather than performs, the reading**: give them one note
  per paper (method, finding, which report section it serves, confidence) so
  they can check your judgment instead of re-reading 40 PDFs to discover what
  you found.
- **Never** produce a finished submission that substitutes for the human's own
  argument: drafts are drafts, and the human decides what the report claims.
  There is no shortcut around the loop — comprehension and source integrity are
  not guaranteed by one.

## Running a session

### 0. Ground the idea/topic

Get from the user (ask if missing): the idea/topic in their own words, the
subject context, and any constraints (date range, source types, language).
Note explicitly that the idea is probably *in its infancy* and they are a
*non-expert yet*, so keyword derivation must be treated as provisional by
design.

### 1. Round One — derive keywords

- Derive a first keyword set **from the user's own phrasing**: core concept
  terms, synonyms, broader/narrower terms, British/US spellings, known
  jargon variants.
- Flag the two risks for this round:
  1. do these keywords accurately characterise an idea that is still
     immature? (they won't fully — that's what rounds are for)
  2. are these the keywords the research community actually uses, or only
     the user's words? (terminology/ontology/language variations)

### 2. { Search → Download → Read → Write } — one round

Run each keyword combination through the researcher server (see *Tooling*
above). Record which combination produced what — the combination, not just the
query.

- **Search**: one `researcher_search_papers` call per keyword combination.
  Vary `sources` deliberately (e.g. `["arxiv"]` for preprints,
  `["pubmed"]` for biomedical) — it changes what you find, so it is worth a
  separate round entry. Record `by_source` so a thin round is visibly thin.
- **Expand**: for the strongest hit, `researcher_references` on its identifier
  gives the prior art (what the field already knew), and
  `researcher_citations` gives what came later. Both are cheap, high-yield
  keyword mines — the titles are the field's own vocabulary.
- **Download**: `researcher_download_pdf` for the open-access PDFs (abstracts
  at minimum for triage). Keep a source list with identifiers
  (DOI/arXiv id/PMID + local path).
- **Read**: actually read the retrieved sources — abstracts+conclusions at
  minimum for triage, methods/results for the keepers. Open local PDFs with
  `pdf-mcp` and HTML with `webfetch`. Do **not** hand the reading back to the
  user: produce one note per paper (method, finding, relevance, section it
  serves) and flag thin or abstract-only evidence. The note is what the user
  audits; an unread paper is reported as unread, never as read.
- **Write**: append those notes per source to the round's notes file (see
  *Round log* below). Understanding is only "updated" once something is
  written down.

### 3. Update understanding → refine → next round

- State what changed: confirmed, corrected, or replaced beliefs; new
  sub-questions.
- **Mine new keywords** from the reading: author terminology, index terms,
  dataset/tool names, abbreviations, the vocabulary the field actually
  uses. This is where round N+1's keywords come from — **refined** keywords,
  not the original guesses.
- Only open a new round if the last one was not yet saturated; say what
  would have to change to saturate.

## Round log (the skill's state file)

Maintain one markdown file, default `research-rounds-<topic-slug>.md` in the
current directory. If the user asks for it somewhere else, write it where
they specify.

Per round, keep a table:

```markdown
## Round N — <date>
**Keywords tried:** …
**Searches run / sources found:** …
**Read & written up:** …
**Understanding updated (what changed):** …
**New terms discovered → next round's keywords:** …
**Saturation?** no — reason: …
```

Resume from the log when continuing a session; never restart at Round One if
rounds already exist.

## Honesty rules

- Cite each claim to a source actually read (title/URL); no invented papers,
  results, or page numbers.
- If a source cannot be retrieved, report it as skipped — do not summarize
  from its abstract snippet alone and imply it was read. `search_papers`
  reporting a source under `errors` means that source was *not* searched, so
  its absence from the results is not evidence of absence in the literature.
- Keyword sets you propose are hypotheses, not findings; label them as
  "Round N candidate keywords" until the user has judged them (only the human
  can say whether they represent the idea).
