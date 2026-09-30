"""``python -m researcher_python`` — run the CLI without installing it.

The installed console script is ``researcher-python`` (see
``pyproject.toml``), but that only exists once the package is built into an
environment.  This module makes the same entry point available from a plain
checkout, which is what ``nix develop`` gives you::

    python -m researcher_python sources --format json
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
