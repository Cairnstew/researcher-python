"""Logging setup for researcher-python.

Kept separate from the CLI so an embedding application can configure the
same way without going through ``argparse``.
"""

from __future__ import annotations

import logging


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure a stream handler on the package root logger.

    Idempotent: repeated calls replace the package logger's handlers rather
    than stacking duplicates.
    """
    logger = logging.getLogger("researcher_python")
    logger.setLevel(level.upper())

    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    return logger
