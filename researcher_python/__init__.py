"""researcher-python — one pipeline across arXiv, OpenAlex, Semantic Scholar, Crossref and PubMed.

Search several scholarly APIs with a single call, merge the results into one
:class:`~researcher_python.models.Paper` record per work, export them as JSON
or JSON Lines, and fetch a PDF when one is open.

    from researcher_python import search, to_json

    result = search("transformer attention")
    to_json(result.papers, "papers.json")

Nothing here performs network I/O at import time: sources are registered by
path and only imported when a source is actually asked for, so
``import researcher_python`` stays light and never pulls in ``arxiv``,
``habanero`` or ``Bio`` (see ``tests/test_docs.py``).  ``web_search`` reaches
structured *web* sources (Stack Overflow, later others) behind the separate
:mod:`researcher_python.web_sources` registry and :class:`WebResult` model.

Configuration comes from :func:`researcher_python.config.load_settings` and
the ``RESEARCHER_*`` environment variables documented in ``.env.example``.
"""

from __future__ import annotations

import logging

__version__ = "0.1.0"

from .citation import (
    CITATION_SORT_MODES,
    CITATION_SOURCES,
    CitationResult,
    citations,
    references,
    sort_citations,
)
from .config import ENV_KEYS, Settings, load_settings
from .download import DownloadResult, download_pdf
from .errors import (
    ConfigurationError,
    DownloadError,
    ExportError,
    NoPdfError,
    NotFoundError,
    RateLimitError,
    ResearcherError,
    SourceError,
    SourceUnavailableError,
    UnsupportedOperation,
    WorkspaceError,
)
from .export import (
    SCHEMA_VERSION,
    Envelope,
    from_json,
    from_jsonl,
    read_envelope,
    to_json,
    to_jsonl,
)
from .format import (
    bibtex_entry,
    bibtex_key,
    csl_item,
    render_bibtex,
    render_csl_json,
    render_ris,
    ris_entry,
    to_bibtex,
    to_csl_json,
    to_ris,
)
from .extract import extract_text, find_quotes, quote_snippets
from .format import (
    harvard_author_sort_key,
    harvard_citation,
    harvard_citation_map,
    harvard_entry,
    render_harvard,
    to_harvard,
)
from .matrix import (
    LITERATURE_COLUMNS,
    column_names,
    matrix_row,
    matrix_rows,
    render_matrix_csv,
    render_matrix_markdown,
    render_synthesis_csv,
    render_synthesis_markdown,
    screen_summary,
    screening_report,
    synthesis_matrix,
    to_matrix_csv,
    to_matrix_markdown,
    to_synthesis_csv,
    to_synthesis_markdown,
)
from .models import (
    SORT_MODES,
    TRIAGE_LABELS,
    Author,
    MergeOutcome,
    Paper,
    PaperIndex,
    dedupe,
    merge,
    merge_sets,
    sort_papers,
)
from .prisma import prisma_report, render_prisma_markdown
from .screening import (
    DEFAULT_EXCLUDED_TYPES,
    DEFAULT_PRESCREEN_RULES,
    LabelUpdate,
    PrescreenCriteria,
    PrescreenHit,
    PrescreenReport,
    UpdateOutcome,
    apply_updates,
    index_papers,
    prescreen,
)
from .snowball import (
    DIRECTIONS,
    Candidate,
    SnowballResult,
    normalize_direction,
    expand as snowball_expand,
)
from .relevance import (
    DEFAULT_MIN_SCORE,
    DEFAULT_TOP_RATIO,
    filter_by_score,
    query_terms,
    score_paper,
    score_papers,
)
from .sources.base import SEARCH_FIELDS, SearchFilters
from .pipeline import PipelineResult, get, search
from .sources import DEFAULT_SOURCES, create_source, get_source_class, source_names
from .workspace import (
    LOG_JSONL,
    LOG_MD,
    META_FILE,
    RESULTS_FILE,
    WORKSPACE_SCHEMA_VERSION,
    SearchLog,
    SearchParams,
    Workspace,
    WorkspaceSearch,
    import_workspace,
    log_entry_for,
    parse_legacy_log,
    render_log_markdown,
    resolve_workspace_path,
)
from .web_sources import (
    DEFAULT_WEB_SOURCES,
    WEB_SOURCES,
    WebResult,
    WebSearchResult,
    WebSource,
    create_web_source,
    render_web_bibtex,
    render_web_csl_json,
    render_web_ris,
    web_bibtex_entry,
    web_bibtex_key,
    web_csl_item,
    web_search,
    web_source_names,
    web_to_bibtex,
    web_to_csl_json,
    web_to_ris,
)

# Library-safe default: attach a NullHandler so that importing the package
# never configures logging or emits output.  Call setup_logging() from the
# CLI entry-point (or your application) to attach real handlers.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "__version__",
    # records
    "Author",
    "Paper",
    "PaperIndex",
    # configuration
    "ENV_KEYS",
    "Settings",
    "load_settings",
    # pipeline
    "DEFAULT_SOURCES",
    "PipelineResult",
    "SEARCH_FIELDS",
    "SORT_MODES",
    "SearchFilters",
    "TRIAGE_LABELS",
    "MergeOutcome",
    "create_source",
    "dedupe",
    "get",
    "get_source_class",
    "merge",
    "merge_sets",
    "search",
    "sort_papers",
    "source_names",
    # screening primitives
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
    # snowballing
    "DIRECTIONS",
    "Candidate",
    "SnowballResult",
    "normalize_direction",
    "snowball_expand",
    # PRISMA
    "prisma_report",
    "render_prisma_markdown",
    # workspace (state + provenance)
    "LOG_JSONL",
    "LOG_MD",
    "META_FILE",
    "RESULTS_FILE",
    "WORKSPACE_SCHEMA_VERSION",
    "SearchLog",
    "SearchParams",
    "Workspace",
    "WorkspaceSearch",
    "import_workspace",
    "log_entry_for",
    "parse_legacy_log",
    "render_log_markdown",
    "resolve_workspace_path",
    # citations
    "CITATION_SORT_MODES",
    "CITATION_SOURCES",
    "CitationResult",
    "citations",
    "references",
    "sort_citations",
    # export
    "SCHEMA_VERSION",
    "Envelope",
    "from_json",
    "from_jsonl",
    "read_envelope",
    "to_json",
    "to_jsonl",
    # format (bibliography)
    "bibtex_entry",
    "bibtex_key",
    "csl_item",
    "render_bibtex",
    "render_csl_json",
    "render_ris",
    "ris_entry",
    "to_bibtex",
    "to_csl_json",
    "to_ris",
    # format (Harvard — the coursework-required style)
    "harvard_author_sort_key",
    "harvard_citation",
    "harvard_citation_map",
    "harvard_entry",
    "render_harvard",
    "to_harvard",
    # matrix (literature / synthesis matrices)
    "LITERATURE_COLUMNS",
    "column_names",
    "matrix_row",
    "matrix_rows",
    "render_matrix_csv",
    "render_matrix_markdown",
    "render_synthesis_csv",
    "render_synthesis_markdown",
    "screen_summary",
    "screening_report",
    "synthesis_matrix",
    "to_matrix_csv",
    "to_matrix_markdown",
    "to_synthesis_csv",
    "to_synthesis_markdown",
    # relevance (local cross-source scoring)
    "DEFAULT_MIN_SCORE",
    "DEFAULT_TOP_RATIO",
    "filter_by_score",
    "query_terms",
    "score_paper",
    "score_papers",
    # extract (PDF text and quotes)
    "extract_text",
    "find_quotes",
    "quote_snippets",
    # download
    "DownloadResult",
    "download_pdf",
    # web sources (Q&A sites, forums)
    "DEFAULT_WEB_SOURCES",
    "WEB_SOURCES",
    "WebResult",
    "WebSearchResult",
    "WebSource",
    "create_web_source",
    "render_web_bibtex",
    "render_web_csl_json",
    "render_web_ris",
    "web_bibtex_entry",
    "web_bibtex_key",
    "web_csl_item",
    "web_search",
    "web_source_names",
    "web_to_bibtex",
    "web_to_csl_json",
    "web_to_ris",
    # errors
    "ConfigurationError",
    "DownloadError",
    "ExportError",
    "NoPdfError",
    "NotFoundError",
    "RateLimitError",
    "ResearcherError",
    "SourceError",
    "SourceUnavailableError",
    "UnsupportedOperation",
    "WorkspaceError",
]
