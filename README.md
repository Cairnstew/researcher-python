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
| `RESEARCHER_OUTPUT_DIR` | optional | where `download` writes PDFs (default `downloads`) |
| `RESEARCHER_LOG_LEVEL` | optional | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `RESEARCHER_MAX_RESULTS` | optional | per-source cap for one search (default 50) |
| `RESEARCHER_RETRIES` | optional | extra attempts after a 429 (default 1) |

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

# Fetch PDFs for one or more identifiers
python -m researcher_python download 1706.03762 10.1103/PhysRevLett.116.061102 \
    --output-dir papers
```

`search` accepts `--format {table,json,jsonl}` and `--out FILE` (JSON/JSONL
only; combining `--out` with `table` is a usage error). `--log-level
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
then reports whether it builds and which capabilities it declares.

## Known limitations

- `Bio.Entrez` stores its `email`/`tool`/`api_key` **process-globally**, so two
  `PubMedSource` instances configured differently in one process clobber each
  other.
- Semantic Scholar effectively needs an API key; keyless requests are
  frequently HTTP 429 from shared IPs.
- `crossref` and `semantic_scholar` are implemented and fixture-tested but not
  yet verified against the live APIs (deferred to `scripts/verify_live.py`).
- No PDF is downloaded for a record with no open-access copy: `pdf_url` is
  empty, Unpaywall has no location, and `NoPdfError` is raised.
