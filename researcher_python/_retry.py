"""Rate-limit retry, shared by the pipeline and the citation fan-out (D8).

Adapters never loop on failure; a :class:`~researcher_python.errors.RateLimitError`
is retried *once layer up* by whichever fan-out called the adapter.  That layer
used to be only :mod:`researcher_python.pipeline`; now it is also
:mod:`researcher_python.citation`.  The loop lives here so the two share one
behaviour, and each caller passes its own ``sleep`` so tests can stub the wait
(``pipeline._sleep`` / ``citations._sleep``) without touching this module.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from .config import Settings
from .errors import RateLimitError, ResearcherError

__all__ = ["DEFAULT_RETRY_AFTER", "MAX_SLEEP", "retry_after", "with_retry"]

T = TypeVar("T")

#: ``Retry-After``/default sleep for a rate-limited source, capped at this.
MAX_SLEEP = 10.0
#: Delay used when the error carries no usable ``Retry-After``.
DEFAULT_RETRY_AFTER = 2.0


def retry_after(exc: ResearcherError) -> float | None:
    """Seconds from ``exc.retry_after``, or ``None`` if it is unusable."""
    value = getattr(exc, "retry_after", None)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def with_retry(
    operation: Callable[[], T],
    settings: Settings,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``operation``, retrying only :class:`RateLimitError`.

    ``settings.retries`` counts *extra* attempts after the first, so
    ``retries=0`` means a rate-limited source fails on its first call.  The
    wait between attempts is ``min(Retry-After or 2, 10)`` seconds.
    """
    attempts = 1 + int(settings.retries)
    for attempt in range(attempts):
        try:
            return operation()
        except RateLimitError as exc:
            if attempt + 1 >= attempts:
                raise
            sleep(min(retry_after(exc) or DEFAULT_RETRY_AFTER, MAX_SLEEP))
    raise AssertionError("unreachable: loop always returns or raises")  # pragma: no cover
