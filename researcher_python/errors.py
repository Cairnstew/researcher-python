"""Exception hierarchy for researcher-python.

Every error raised by this package derives from :class:`ResearcherError`, so
an embedding application can catch one type at the boundary.  Source-level
failures additionally carry the name of the source that raised them, which is
what the pipeline needs in order to report "3 of 5 sources failed" instead of
swallowing a bare ``requests`` exception.
"""

from __future__ import annotations


class ResearcherError(Exception):
    """Base class for every error raised by researcher-python."""


class ConfigurationError(ResearcherError):
    """A required setting is missing or malformed."""


class SourceUnavailableError(ResearcherError):
    """The named source could not be constructed.

    Raised when a source's third-party dependency is not importable, or when
    it needs a credential that has not been configured.
    """

    def __init__(self, source: str, reason: str = "") -> None:
        self.source = source
        self.reason = reason
        message = f"source {source!r} is unavailable"
        if reason:
            message = f"{message}: {reason}"
        super().__init__(message)


class SourceError(ResearcherError):
    """A source returned a transport-level or API-level failure."""

    def __init__(
        self,
        source: str,
        message: str = "",
        *,
        cause: BaseException | None = None,
    ) -> None:
        self.source = source
        self.cause = cause
        super().__init__(message or f"{source}: request failed")
        if cause is not None:
            self.__cause__ = cause


class NotFoundError(SourceError):
    """The source recognised the request but has no such record."""


class RateLimitError(SourceError):
    """The source throttled or refused the request for exceeding its quota.

    ``retry_after`` carries the server's own ``Retry-After`` hint in seconds
    (already parsed, so an HTTP-date form arrives as ``None``); the pipeline
    clamps it before sleeping.
    """

    def __init__(
        self,
        source: str,
        message: str = "",
        *,
        retry_after: float | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.retry_after = retry_after
        super().__init__(source, message, cause=cause)


class UnsupportedOperation(ResearcherError):
    """The source does not implement the operation that was requested."""


class ExportError(ResearcherError):
    """A result set could not be rendered in the requested format."""

    def __init__(self, message: str = "", *, cause: BaseException | None = None) -> None:
        self.cause = cause
        super().__init__(message or "export failed")
        if cause is not None:
            self.__cause__ = cause


class WorkspaceError(ResearcherError):
    """A workspace is missing, corrupt, or cannot be written.

    Raised by :mod:`researcher_python.workspace` when its files disagree or a
    result set cannot be persisted.  It is deliberately distinct from
    :class:`ExportError`: an export failure is about one format, a workspace
    failure means the provenance store itself is not trustworthy.
    """

    def __init__(self, message: str = "", *, cause: BaseException | None = None) -> None:
        self.cause = cause
        super().__init__(message or "workspace operation failed")
        if cause is not None:
            self.__cause__ = cause


class DownloadError(ResearcherError):
    """A PDF (or other artifact) could not be fetched or written."""


class NoPdfError(DownloadError):
    """No open-access PDF URL could be resolved for the record."""


__all__ = [
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
