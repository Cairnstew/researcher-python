"""Fetch the PDF for a :class:`~researcher_python.models.Paper`.

Resolution walks two candidates and stops at the first one that actually
yields PDF bytes:

1. ``paper.pdf_url`` — whatever the adapter already knew (``via="paper"``).
2. Unpaywall, by DOI (``via="unpaywall"``).

There is deliberately no "ask the adapter for a URL" step (D9): there is no
``Source.pdf()``.  An adapter exposes a PDF by setting ``Paper.pdf_url``,
which is exactly what ``Capabilities.pdf`` means — *this adapter populates
``Paper.pdf_url`` when an open PDF is known*.

Two rules shape the rest:

* **HTML landing pages are common.**  A URL that resolves to 200 with an HTML
  body is a *failed candidate*, not a success, so the first bytes are checked
  for ``%PDF-`` before the file is published.
* **Nothing is half-written.**  Bytes stream to ``<name>.pdf.part`` and are
  renamed into place only after the magic check passes; the ``.part`` is
  removed on every failure path.

The HTTP layer is :data:`_http_get`, a module-level indirection so tests
never touch the network.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .config import ENV_KEYS, Settings
from .errors import (
    ConfigurationError,
    DownloadError,
    NoPdfError,
    RateLimitError,
    SourceError,
)
from .ids import wire_doi
from .models import Paper

__all__ = ["DownloadResult", "download_pdf", "parse_unpaywall"]

#: Indirection so tests never touch the network.
_http_get = requests.get

_UNPAYWALL = "https://api.unpaywall.org/v2"
#: Literal name reported for Unpaywall failures (it is not a registry source).
_UNPAYWALL_SOURCE = "unpaywall"
#: A landing page that answers 200 with HTML is a failed candidate.
_PDF_MAGIC = b"%PDF-"
_CHUNK = 65536
#: ``slug`` cap, leaving room for the id suffix inside a sane filename length.
_SLUG_LEN = 60


@dataclass(slots=True)
class DownloadResult:
    """Where a PDF landed, which candidate produced it, and from where."""

    path: Path
    url: str
    #: One of ``paper``, ``unpaywall``, or ``existing`` (already on disk and
    #: ``overwrite=False``, in which case no network call ran).
    via: str


# ---------------------------------------------------------------------- #
# Pure helpers (D4)
# ---------------------------------------------------------------------- #


def parse_unpaywall(d: Mapping[str, Any]) -> list[str]:
    """Candidate PDF URLs from a Unpaywall record, best first.

    ``best_oa_location`` wins, then the remaining ``oa_locations``.  Within a
    location ``url_for_pdf`` beats the landing-page ``url``, nulls are
    skipped, and duplicates collapse so the same URL is never tried twice.
    """
    if not isinstance(d, Mapping):
        return []

    urls: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value and value not in urls:
            urls.append(value)

    best = d.get("best_oa_location")
    if isinstance(best, Mapping):
        add(best.get("url_for_pdf"))
        add(best.get("url"))

    locations = d.get("oa_locations")
    if isinstance(locations, list):
        for location in locations:
            if isinstance(location, Mapping):
                add(location.get("url_for_pdf"))
                add(location.get("url"))
    return urls


def _slug(title: str, max_len: int = _SLUG_LEN) -> str:
    """Lowercase ASCII slug; non-ASCII characters are folded then dropped."""
    folded = (
        unicodedata.normalize("NFKD", title or "")
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    return slug[:max_len].strip("-") or "untitled"


def _short_id(paper: Paper) -> str:
    """First 8 hex chars of the SHA-1 of the best identifier, for the filename."""
    for candidate in (paper.doi, paper.arxiv_id, paper.pmid, paper.title):
        if candidate:
            return hashlib.sha1(candidate.encode("utf-8")).hexdigest()[:8]
    return hashlib.sha1(b"").hexdigest()[:8]


def _filename(paper: Paper) -> str:
    return f"{_slug(paper.title)}-{_short_id(paper)}.pdf"


def _env_name(field: str) -> str:
    """Env var feeding ``field``, read from :data:`config.ENV_KEYS` (never hardcoded)."""
    for env_name, name in ENV_KEYS.items():
        if name == field:
            return env_name
    return field


# ---------------------------------------------------------------------- #
# Resolution
# ---------------------------------------------------------------------- #


def download_pdf(
    paper: Paper,
    output_dir: str | os.PathLike[str] | None = None,
    *,
    overwrite: bool = False,
    settings: Settings | None = None,
) -> DownloadResult:
    """Write ``paper``'s PDF to disk and report which candidate produced it.

    Raises :class:`~researcher_python.errors.NoPdfError` when no candidate URL
    exists at all, and :class:`~researcher_python.errors.DownloadError` —
    listing every candidate and why it failed — when candidates existed but
    none produced a PDF.
    """
    settings = settings if settings is not None else Settings()

    target = Path(output_dir) if output_dir is not None else Path(settings.output_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = target / _filename(paper)

    if path.exists() and not overwrite:
        # Already fetched: no network call at all.
        return DownloadResult(path=path, url=paper.pdf_url, via="existing")

    failures: list[str] = []
    resolved = 0  # candidate URLs we actually obtained, across both steps
    part = path.with_name(f"{path.name}.part")

    def attempt(url: str, via: str) -> DownloadResult | None:
        """Try one candidate; a failure is recorded and the next one runs."""
        try:
            if not _stream_pdf(url, part, float(settings.timeout)):
                failures.append(f"{via}: {url} -> response was not a PDF")
                return None
            os.replace(part, path)
            return DownloadResult(path=path, url=url, via=via)
        except Exception as exc:  # noqa: BLE001 - reported, then the next candidate
            failures.append(f"{via}: {url} -> {exc}")
            return None
        finally:
            # Never leave a partial file behind, even after a successful rename.
            part.unlink(missing_ok=True)

    # 1. Whatever the adapter already put on the record.
    if paper.pdf_url:
        resolved += 1
        result = attempt(paper.pdf_url, "paper")
        if result is not None:
            return result

    # 2. Unpaywall, by DOI — only reached when step 1 had no URL or a bad one.
    if paper.doi:
        for url in _unpaywall_urls(paper, settings):
            resolved += 1
            result = attempt(url, "unpaywall")
            if result is not None:
                return result

    if resolved == 0:
        detail = f" ({'; '.join(failures)})" if failures else ""
        raise NoPdfError(f"no PDF URL for {paper.title!r}{detail}")

    lines = "\n".join(f"  - {entry}" for entry in failures)
    raise DownloadError(f"every PDF candidate failed for {paper.title!r}:\n{lines}")


def _unpaywall_urls(paper: Paper, settings: Settings) -> list[str]:
    """Unpaywall candidates for ``paper``'s DOI (D3: the DOI goes out as given)."""
    if not settings.email:
        # Unpaywall is the only step left, so the missing credential is a stop.
        raise ConfigurationError(
            f"setting 'email' is required to query Unpaywall; "
            f"export {_env_name('email')} first"
        )

    doi = wire_doi(paper.doi)
    url = f"{_UNPAYWALL}/{quote(doi, safe='/')}?email={quote(settings.email)}"

    try:
        response = _http_get(url, timeout=float(settings.timeout))
    except requests.RequestException as exc:
        raise SourceError(
            _UNPAYWALL_SOURCE, f"Unpaywall request failed: {exc}", cause=exc
        ) from exc

    status = _status_of(response)
    if status == 404:
        # Unpaywall does not know the DOI — that is "no candidates", not an error.
        return []
    if status == 429:
        raise RateLimitError(
            _UNPAYWALL_SOURCE, "Unpaywall rate limit exceeded", cause=_http_error(response)
        )
    if not 200 <= status < 300:
        raise SourceError(
            _UNPAYWALL_SOURCE,
            _response_text(response) or f"Unpaywall returned HTTP {status}",
            cause=_http_error(response),
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise SourceError(
            _UNPAYWALL_SOURCE, "Unpaywall returned non-JSON", cause=exc
        ) from exc
    return parse_unpaywall(payload)


# ---------------------------------------------------------------------- #
# Transport
# ---------------------------------------------------------------------- #


def _stream_pdf(url: str, part: Path, timeout: float) -> bool:
    """Stream ``url`` into ``part``; ``True`` only if the bytes are a PDF."""
    response = _http_get(url, stream=True, timeout=timeout)
    try:
        status = _status_of(response)
        if not 200 <= status < 300:
            raise SourceError(
                "download",
                _response_text(response) or f"{url} returned HTTP {status}",
            )
        with open(part, "wb") as handle:
            for chunk in response.iter_content(chunk_size=_CHUNK):
                if chunk:
                    handle.write(chunk)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    with open(part, "rb") as handle:
        head = handle.read(len(_PDF_MAGIC))
    if head != _PDF_MAGIC:
        return False
    return True


def _status_of(response: Any) -> int:
    try:
        return int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _response_text(response: Any) -> str:
    text = getattr(response, "text", "")
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())[:200]


def _http_error(response: Any) -> BaseException | None:
    """Build an exception carrying the status, for ``cause=``."""
    status = _status_of(response)
    if not status:
        return None
    return RuntimeError(f"HTTP {status}")
