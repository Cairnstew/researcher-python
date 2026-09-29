"""researcher-python — Python project scaffold (purpose TBD).

The real purpose of this package is not decided yet; the scaffold exists so
the house layout (pyproject + Nix flake + offline tests + thin CLI) is in
place before the first real module lands.  Replace this docstring and the
package description in ``pyproject.toml`` / ``nix/default.nix`` when the
purpose is settled.
"""

from __future__ import annotations

import logging

__version__ = "0.1.0"

# Library-safe default: attach a NullHandler so that importing the package
# never configures logging or emits output.  Call setup_logging() from the
# CLI entry-point (or your application) to attach real handlers.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = ["__version__"]
