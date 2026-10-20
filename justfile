# researcher-python — development recipes
# Docs: https://just.systems/man/en/

# Every tool recipe runs inside the flake's Nix dev shell, so the only tools
# needed on PATH are `just` and `nix`: python, pytest and the runtime deps come
# from the flake, not from the host.  `nix develop --command bash -c '<cmd>'`
# passes the recipe body as one string, so flags and quoting reach the command
# untouched.  Same convention as the kraken-trading-bot / kurosearch justfiles.
# Recipes that only drive `nix` (build/check/dev/list) do not use it.
dev := "nix develop --command bash -c"

# ── Defaults ───────────────────────────────────────────────────────────────

# Recipe to run when `just` is called with no arguments.
default: test

# ── Testing ────────────────────────────────────────────────────────────────

# Run the full offline test suite (no network).
test:
    {{dev}} 'pytest tests/ -q'

# Run tests with verbose output (per-test names).
test-verbose:
    {{dev}} 'pytest tests/ -v'

# Run a single test file or node, e.g. `just test-one tests/test_cli.py`.
test-one PATTERN:
    {{dev}} 'pytest {{PATTERN}} -v'

# Run tests matching a keyword, e.g. `just test-kw pipeline`.
test-kw KEYWORD:
    {{dev}} 'pytest tests/ -k {{KEYWORD}} -v'

# ── Nix ────────────────────────────────────────────────────────────────────

# Build the package via Nix.
build:
    nix build

# Run all flake checks (build + tests + dev shell).
check:
    nix flake check

# Fail on untracked source files, then evaluate and build the flake.
preflight:
    out=$(git ls-files --others --exclude-standard -- researcher_python tests nix scripts .opencode); if [ -n "$out" ]; then echo "untracked source files (git add them):"; echo "$out"; exit 1; fi
    nix flake check

# Regenerate uv.lock after editing pyproject.toml (the only dependency source).
lock:
    nix shell nixpkgs#uv --command uv lock

# Install the pre-commit and pre-push hooks.
hooks:
    nix shell nixpkgs#pre-commit --command pre-commit install -t pre-commit -t pre-push

# Enter the dev shell (Python + deps + pytest on PATH).
dev:
    nix develop

# ── Live verification ──────────────────────────────────────────────────────

# Dry-run: show what verify_live would check (no network).
verify-dry:
    {{dev}} 'python scripts/verify_live.py --dry-run'

# Verify all five sources against the live APIs (needs unthrottled network).
verify-live *ARGS:
    {{dev}} 'python scripts/verify_live.py {{ARGS}}'

# Run the live research script: search -> bibliography (--refs/--pdf opt-in).
research *ARGS:
    {{dev}} 'python scripts/research.py {{ARGS}}'

# Search a web source (Stack Overflow by default), e.g.
# `just web-search '"merge dicts"' --max 5 --sort votes --site serverfault`.
web-search *ARGS:
    {{dev}} 'python scripts/web_search.py {{ARGS}}'

# Search with provenance: appends a round-log row per search (lab03's
# "record which combination produced what").  e.g.
#   just search-log research-out/rounds.md "algorithmic bias" --field title
search-log LOG *ARGS:
    {{dev}} 'python -m researcher_python search {{ARGS}} --search-log {{LOG}}'

# ── Workspaces (results + provenance) ─────────────────────────────────────

# Create a workspace, e.g. `just ws-init research-out/cw1 --topic "AI ethics"`.
ws-init PATH *ARGS:
    {{dev}} 'python -m researcher_python workspace init {{PATH}} {{ARGS}}'

# Counts, round, provenance and paths.
ws-status PATH *ARGS:
    {{dev}} 'python -m researcher_python workspace status {{PATH}} {{ARGS}}'

# Print the provenance log (md by default; --format jsonl|json, --round N).
ws-log PATH *ARGS:
    {{dev}} 'python -m researcher_python workspace log {{PATH}} {{ARGS}}'

# Snapshot and advance a round, e.g. `just round research-out/cw1 --next`.
round PATH *ARGS:
    {{dev}} 'python -m researcher_python workspace round {{PATH}} {{ARGS}}'

# Search into a workspace, e.g. `just search-ws research-out/cw1 '"query"'`.
search-ws PATH *ARGS:
    {{dev}} 'python -m researcher_python search {{ARGS}} --workspace {{PATH}}'

# Screening workflow: screen, pre-screen, snowball, matrix, synthesis, summary.
# e.g. `just screen results.json --set core`
screen *ARGS:
    {{dev}} 'python -m researcher_python screen {{ARGS}}'

prescreen *ARGS:
    {{dev}} 'python -m researcher_python prescreen {{ARGS}}'

snowball *ARGS:
    {{dev}} 'python -m researcher_python snowball {{ARGS}}'

prisma *ARGS:
    {{dev}} 'python -m researcher_python prisma {{ARGS}}'

matrix *ARGS:
    {{dev}} 'python -m researcher_python matrix {{ARGS}}'

synthesis *ARGS:
    {{dev}} 'python -m researcher_python synthesis {{ARGS}}'

summary *ARGS:
    {{dev}} 'python -m researcher_python summary {{ARGS}}'

refs *ARGS:
    {{dev}} 'python -m researcher_python bibliography {{ARGS}}'

# Extract text/quotes from downloaded PDFs, e.g. `just extract papers --quotes`.
extract *ARGS:
    {{dev}} 'python -m researcher_python extract {{ARGS}}'

# ── CLI ────────────────────────────────────────────────────────────────────

# Run the CLI, e.g. `just cli -- search --source arxiv --max 3`.
cli *ARGS:
    {{dev}} 'python -m researcher_python {{ARGS}}'

# ── MCP server ─────────────────────────────────────────────────────────────

# Serve the MCP tools over stdio (an MCP client spawns this itself; run it by
# hand only to smoke-test that it starts).
mcp:
    {{dev}} 'researcher-python-mcp'

# ── Maintenance ────────────────────────────────────────────────────────────

# Remove Python caches and build artifacts.
clean:
    find . -type d -name __pycache__ -exec rm -rf {} +
    find . -type f -name '*.pyc' -delete
    rm -rf .pytest_cache .ruff_cache dist build *.egg-info

# ── Meta ───────────────────────────────────────────────────────────────────

# List all available recipes.
list:
    just --list
