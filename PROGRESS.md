# PROGRESS.md — researcher-python

> **Agent: read this file FIRST after any compaction/restart. Update it after every completed unit of work (before moving on). Keep it short; edit in place, don't append essays.**

## Resume here

**Current step:** all checklist items done; remaining manual steps below
**Next action:** nothing automated left. Do NOT push — this repo stays manual-push.
**Blocked on:** nothing.
**Just done:** item 10 — committed the whole tree as 14 conventional commits (tree `da9a5156`). Two cleanups preceded it: dropped the dead `base != "*.log"` source-filter condition in `nix/default.nix`, and `info` now shows an unset `email` as `unset` (text) / `null` (json) instead of a blank cell — API keys are still only ever `set`/`unset`. **523 tests green.**

### Remaining manual steps

- run `scripts/verify_live.py --source crossref --source semantic_scholar` from an unthrottled network, report results
- aarch64-linux and darwin builds not exercised

## Report format (agent: end EVERY finished step with exactly this block, nothing after it)

```
=== REPORT ===
STEP: <checklist item number + name>
STATUS: done | partial | blocked
CHANGED: <files added/modified, one line each>
VERIFIED: <commands run + pass/fail, e.g. pytest: 14 passed>
DECISIONS/DEVIATIONS: <anything not covered by D1-D7, or "none">
OPEN QUESTIONS/RISKS: <max 3 bullets, or "none">
NEXT (per checklist): <next item number + name>
=== END REPORT ===
```

Keep it under ~25 lines. Do not paste code or long logs; include only the first failing traceback tail if something failed.
The user relays this block to Claude, who returns the next prompt. Do not begin the next item until a new prompt arrives.

## What this is

Unified Python wrapper + CLI to search/fetch scholarly papers across 5 sources (`arxiv`, `openalex`, `semantic_scholar`, `crossref`, `pubmed`) behind one `Paper` model and one pipeline.

## Decisions (final — do not relitigate)

- **D1** Write Crossref + Semantic Scholar adapters now; defer live verification to `scripts/verify_live.py` (user runs it later from a non-rate-limited IP). Mark those adapters `unverified-live` below.
- **D2** Declare `httpx` explicitly in all 3 places (`pyproject.toml`, `nix/default.nix`, `flake.nix`) — don't rely on habanero's transitive dep.
- **D3** DOIs are case-insensitive: `normalize_doi` may lowercase for _keys/dedupe_, but send the DOI to APIs as given by the user (stripped of URL prefix). Do not "fix" mixed-case DOIs.
- **D4** Parse functions are pure (dict/XML in → `Paper` out) so tests use fixtures, never the network.
- **D5** Missing/unavailable adapter must raise `SourceUnavailableError`, not `ModuleNotFoundError` (fix in `get_source_class`).
- **D6** PubMed XPaths must be strictly scoped `./MedlineCitation/…` (never unscoped `.//`).
- **D7** Semantic Scholar: keyless endpoints 429 persistently → key handling is load-bearing; raise `ConfigurationError`/clear message when key needed.
- **D8** HTTP 5xx from a source -> `SourceError`. `SourceUnavailableError` only for missing/unconstructable adapters (D5). No retry loops inside adapters; retry/backoff, if wanted, lives at pipeline level (item 4a).
- **D9** There is no `Source.pdf()`. Adapters expose a PDF by setting `Paper.pdf_url`; `Capabilities.pdf` means "this adapter populates `Paper.pdf_url` when an open PDF is known". `download.py` uses `pdf_url`, then Unpaywall.

## Done (verified)

**Adapters — all five written; live-verified status:**

- `arxiv_source.py` — live-verified: **yes** (search, get)
- `openalex_source.py` — live-verified: **yes** (search, get, references, citations)
- `crossref_source.py` — live-verified: **no** (unverified-live, D1; 429 from dev IP)
- `pubmed_source.py` — live-verified: **yes** (n=2 search probe)
- `semantic_scholar_source.py` — live-verified: **no** (unverified-live, D1)

**Also done:**

- Item 5a+5b: `researcher_python/cli/` package — `__init__.py` (argparse `build_parser` + `main(argv)->int` + shared `_report` handler: stdout for data, stderr for errors, exit 0/1/2, `ConfigurationError`→2 else→1, single line, traceback only at `--log-level DEBUG`) and `commands.py` (`sources`, `info`, `search`, `get`, `download`); `search` -> `pipeline.search` with `--source/--max/--sort/--format/--out`, `get` -> detail view or `to_dict()`, `download` -> per-identifier `pipeline.get` + `download_pdf` that continues past failures; `researcher-python` entry point unchanged; `researcher_python/__main__.py` added; `tests/test_cli.py` (33 cases incl. key redaction, exit codes, `python -m` subprocess)
- D9 recorded and applied: no `Source.pdf()`; `download.py` is `pdf_url` -> Unpaywall (+ the `existing` short-circuit), `sources=` argument dropped
- Item 4c: `download.py` — `DownloadResult(path, url, via)`; `download_pdf` tries `paper.pdf_url` -> Unpaywall, resolving each step only when the previous one failed; streams to `<name>.pdf.part`, verifies `%PDF-`, then `os.replace`; `via="existing"` short-circuits with no network; filename = ascii slug + first 8 of sha1(best id); `parse_unpaywall` pure; module-level `_http_get` is the only transport; `tests/test_download.py`
- Hardening: `Settings.__post_init__` now also rejects `timeout <= 0` and `max_results < 1`; export's temp file switched from a PID suffix to `tempfile.mkstemp`
- Item 4b: `export.py` — `SCHEMA_VERSION = 1`, `to_json` (envelope: `schema_version`/`generated_at`/`count`/`meta`?/`papers`), `to_jsonl` (one `to_dict()` per line, unversioned), `from_json`/`from_jsonl`; path *or* file object; paths written via sibling temp + `os.replace` with parent dirs created; all failures are `ExportError` (added a `cause=` kwarg to it) naming `index`/`line`; `tests/test_export.py`
- `Settings.retries` (default 1, `RESEARCHER_RETRIES`) — validated non-negative in `__post_init__`, and the pipeline now reads it directly instead of duck-typing; `tests/test_config.py`
- Item 4a: `pipeline.py` — `PipelineResult(papers, errors, by_source)`; `search()` fans out in a `ThreadPoolExecutor`, records construction/capability/transport failures per source instead of aborting, round-robin merges into `dedupe` + `sort_papers`; `get()` routes by identifier type and aggregates per-source failures; D8 retry (rate limits only) lives here behind an injectable `_sleep`; `tests/test_pipeline.py` (fakes registered through the registry — no network)
- Foundation: `errors.py`, `ids.py`, `models.py`, `config.py`, `sources/base.py`, `sources/__init__.py`
- `wire_doi` lives in `ids.py` so the DOI-using adapters share one D3 helper; `Settings.timeout` (30.0) added for the HTTP sources
- Item 0: `httpx>=0.27` / `httpx` in `pyproject.toml` / `nix/default.nix` / `flake.nix` (D2); `get_source_class` → `SourceUnavailableError(name, "not implemented yet")` for an absent adapter, third-party `ModuleNotFoundError` re-raised (D5)
- Deps `arxiv`, `habanero`, `biopython` wired in all 3 places
- Item 9: `nix/default.nix` had `doCheck = false`, so the build never ran the suite (vacuous green). Now `doCheck = true` with an explicit `checkPhase` running `pytest tests/ -q` plus `pytest` in `nativeBuildInputs` — the sandbox is stricter than `nix develop` (no PYTHONPATH, no network, only files in `src`). Second fix: `tests/test_docs.py::test_import_does_not_eagerly_load_heavy_adapters` spawned `python -c` with no `PYTHONPATH`, so it could not resolve the package before `installPhase`; it now injects the checkout root the same way `test_cli.py` already did. `.gitignore` gained `result-*`. Result: 521 green in the dev shell **and** in the sandbox; `nix build` OK; `nix flake check` all checks passed.
- Tests: 521 passing, 0 xfails (offline — the suite makes no network calls)
- `RESEARCHER_TIMEOUT` is a real env var: parsed as a float in `load_settings` (parallel to `_INT_FIELDS`), `Settings.timeout` still validated > 0 with the message naming the variable; `.env.example` documents it and the `tests/test_docs.py` ENV_KEYS guard covers it.
- Item 8: `scripts/verify_live.py` — `main(argv) -> int`, check list from `source.capabilities` (never hardcoded), 8 checks per source incl. crossref-only DOI case-sensitivity, polite `Polite` delay between calls, `RATELIMIT` stops that source's remaining network checks. Outcomes PASS/FAIL/SKIP/RATELIMIT; exit 1 > 3 > 0 (SKIP never fails). `--dry-run`/`--format`/`--delay`/`--timeout`/`--source`. Tests: `tests/test_verify_live.py` (21, script loaded via importlib, fakes in the registry). **Never run against a real source this session.**
- Item 7: `README.md` rewritten from the real CLI/`Capabilities`/`ENV_KEYS` (capability table, one example per subcommand, exit codes, error types, known limitations, Extending); `.env.example` documents all 8 `ENV_KEYS` with where-to-get-them notes; `researcher_python/__init__.py` re-exports 34 names in `__all__` and imports no heavy adapter eagerly; all "scaffold"/"purpose TBD" language removed; `tests/test_docs.py` guards env keys, subcommand names, `__all__`, the capability table and runs the README's Python example offline.
- Item 6 fixes (all four former xfails now normal passing tests): `normalize_orcid` upper-cases before validating the checksum; `year_from_date` is anchored so identifiers cannot yield a year (it has no callers, so nothing changed); `openalex._pmcid` matches `PMC\\d+` case-insensitively in the original string; `RateLimitError` now takes/stores `retry_after` and all four 429 mappers parse `Retry-After` (shared `parse_retry_after` in `sources/base.py`). **Dropped:** `year_from_date("Published in 1999")` — mid-string years are no longer matched, per the anchored-match spec.
- Item 6: audit-driven test pass — `tests/test_ids.py`, `test_models.py`, `test_errors.py`, `test_base.py`, `test_arxiv_parse.py`, `test_openalex_parse.py` added; `test_config.py`/`test_registry.py` extended (whole `_REGISTRY`, no IMPLEMENTED list). The 4 xfails are genuine bugs found, **not fixed** — they are `xfail(strict=True)` so they flip loudly if addressed: `ids.normalize_orcid` rejects a lower-case checksum X (its `.upper()` is unreachable), `ids.year_from_date` reads `1000` out of a DOI, `openalex._pmcid` fails on the URL form its own docstring promises, `RateLimitError` has no `retry_after` (pipeline always falls back to 2s).

## Checklist (tick as done; note any deviation on the line)

- [x] 0. Housekeeping: add `httpx` dep (D2); fix `get_source_class` → `SourceUnavailableError` (D5) — +`tests/test_registry.py`
- [x] 1. `crossref_source.py` (unverified-live) — +fixture +`tests/test_crossref_parse.py`, 42 tests green
- [x] 2. `pubmed_source.py` — live-verified = yes; fixture + D6 regression; 65 tests green
- [x] 3. `semantic_scholar_source.py` (unverified-live) — fixtures + `tests/test_s2_parse.py`, 96 tests green; +`Settings.timeout`
- [x] 4a. `pipeline.py` (fan-out + merge/dedupe) — +`tests/test_pipeline.py`, 114 tests green
- [x] 4b. `export.py` (JSON/JSONL, `SCHEMA_VERSION`) — +`tests/test_export.py`, 159 tests green
- [x] 4c. `download.py` (Unpaywall fallback for DOIs) — +`tests/test_download.py`, 207 tests green (203 after D9 dropped the phantom `Source.pdf()` step)
- [x] 5a. CLI framework + `sources` + `info` subcommands — CLI is now a package, argparse unchanged; 214 tests green
- [x] 5b. CLI `search`, `get`, `download` subcommands — 231 tests green; `python -m researcher_python` entry point added
- [x] 6. Offline tests for all new code (fixtures in `tests/fixtures/`) — 429 passed, 4 strict xfails; +201 tests
- [x] 7. Rewrite `README.md` + `.env.example`; re-exports in `researcher_python/__init__.py` — 493 tests green, 0 xfails
- [x] 8. `scripts/verify_live.py` (all 5 sources, prints pass/fail per capability) — 521 tests green; only `--dry-run`/`--help` exercised here
- [x] 9. Final verify: pytest → nix build → nix flake check — 521 green in the dev shell AND in the sandbox; build + built-artifact smoke tests + flake check all pass
- [x] 10. Commit in logical chunks

## Known gotchas

- Crossref returned HTTP 429 from dev IP even with `mailto` + 1.2s spacing → don't burn tokens probing live.
- PubMed unscoped `.//` XPath bug was already found once — keep scoped.
- Nix: new deps must appear in `pyproject.toml`, `nix/default.nix`, AND `flake.nix`.
- Bio.Entrez email/tool/api_key are process-global; two PubMedSource instances with different settings in one process clobber each other. Accepted limitation; revisit only if the pipeline ever needs it.

## Session log (newest first, max ~10 lines; prune old entries)

- 2026-09-30 — item 10 done: 14 conventional commits covering build/core/base/5 sources/pipeline/export+download/CLI/verify-live/docs/PROGRESS; preceded by 2 cleanups (dead `*.log` filter dropped, `info` email blank -> unset/null) -> 523 passed, tree da9a5156. Nothing pushed.
- 2026-09-30 — item 5b done: `search`/`get`/`download` subcommands reusing 5a's handler (exit 0 when any source answered, 1 when all failed, 2 for ConfigurationError incl. unknown `--source`, bad `--sort` and `--out --format table`); `python -m researcher_python` via new `__main__.py` + subprocess test; 16 new CLI tests → 231 passed. No live calls. Next: item 6, offline test audit.
- 2026-09-30 — item 5a done: CLI split into `researcher_python/cli/` (`__init__.py` = argparse parser + `main` + shared error handler; `commands.py` = `sources`, `info`), entry point `researcher-python` → `researcher_python.cli:main` unchanged, no new deps; `--format table|json` on both, keys shown only as set/unset; 16 new CLI tests. Also: D9 applied (phantom `Source.pdf()` step + `sources=` removed from `download.py`), session log pruned, item 5 split into 5a/5b. No live calls. Next: item 5b.
- 2026-09-30 — item 4c done: `download.py` (candidates with *lazy* fall-through — Unpaywall is only resolved once earlier steps fail; `%PDF-` magic check; `.part` + `os.replace`; slug+sha1 filename; injectable `_http_get`) + 26 tests. Hardening: `Settings` validates `timeout`/`max_results`, export uses `tempfile.mkstemp`. No live calls. Next: item 5, CLI.
- 2026-09-30 — items 4a+4b done: `pipeline.py` (18 tests) and `export.py` (JSON envelope + JSONL, atomic writes, `ExportError(cause=)`, 25 tests); `Settings.retries` added with `RESEARCHER_RETRIES` + `__post_init__` validation and real Settings-based retry tests; `tests/test_config.py` created. No live calls. Next: item 4c, `download.py`.
