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
``habanero`` or ``Bio`` (see ``tests/test_docs.py``).

Configuration comes from :func:`researcher_python.config.load_settings` and
the ``RESEARCHER_*`` environment variables documented in ``.env.example``.
"""

from __future__ import annotations

import logging

__version__ = "0.1.0"

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
)
from .export import (
    SCHEMA_VERSION,
    from_json,
    from_jsonl,
    to_json,
    to_jsonl,
)
from .models import SORT_MODES, Author, Paper, dedupe, merge, sort_papers
from .pipeline import PipelineResult, get, search
from .sources import DEFAULT_SOURCES, create_source, get_source_class, source_names

# Library-safe default: attach a NullHandler so that importing the package
# never configures logging or emits output.  Call setup_logging() from the
# CLI entry-point (or your application) to attach real handlers.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "__version__",
    # records
    "Author",
    "Paper",
    # configuration
    "ENV_KEYS",
    "Settings",
    "load_settings",
    # pipeline
    "DEFAULT_SOURCES",
    "PipelineResult",
    "SORT_MODES",
    "create_source",
    "dedupe",
    "get",
    "get_source_class",
    "merge",
    "search",
    "sort_papers",
    "source_names",
    # export
    "SCHEMA_VERSION",
    "from_json",
    "from_jsonl",
    "to_json",
    "to_jsonl",
    # download
    "DownloadResult",
    "download_pdf",
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
]
