"""Persist papers as JSON or JSON Lines, and read them back.

Two shapes, deliberately different:

* **JSON** is an *envelope* — ``schema_version``, ``generated_at``, ``count``,
  optional ``meta``, then ``papers``.  The version is what makes a round trip
  safe: a reader that meets a newer :data:`SCHEMA_VERSION` refuses the file
  instead of silently dropping fields it does not understand.
* **JSON Lines** is one :meth:`Paper.to_dict` per line with **no envelope**.
  A stream carries no header, so JSONL is *unversioned by construction* — use
  it for piping and appending, and prefer JSON when the file must survive a
  schema bump.

Both accept a path or an already-open text file object.  A path is written
atomically: parent directories are created, output goes to a sibling temp
file, and :func:`os.replace` publishes it only after the write succeeds, so a
crash mid-write never leaves a half-written file behind.

Every failure the reader or writer can produce is an
:class:`~researcher_python.errors.ExportError`; a message that concerns one
record names its ``index`` (JSON) or ``line`` (JSONL).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from .errors import ExportError
from .models import Paper

__all__ = [
    "SCHEMA_VERSION",
    "from_json",
    "from_jsonl",
    "to_json",
    "to_jsonl",
]

#: Bump only when :meth:`Paper.to_dict` changes shape incompatibly.
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------- #
# Writing
# ---------------------------------------------------------------------- #


def to_json(
    papers: Iterable[Paper],
    dest: str | os.PathLike[str] | TextIO,
    *,
    meta: Mapping[str, Any] | None = None,
) -> None:
    """Write ``papers`` to ``dest`` as one versioned JSON envelope."""
    items = list(papers)
    if meta is not None and not isinstance(meta, Mapping):
        raise ExportError(f"meta must be a mapping or None, got {type(meta).__name__}")

    envelope: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_utc(),
        "count": len(items),
    }
    if meta is not None:
        envelope["meta"] = dict(meta)
    envelope["papers"] = [paper.to_dict() for paper in items]

    _dump(
        envelope,
        dest,
        indent=2,
    )


def to_jsonl(papers: Iterable[Paper], dest: str | os.PathLike[str] | TextIO) -> None:
    """Write one :meth:`Paper.to_dict` per line — no envelope, no version."""
    items = list(papers)
    try:
        with _atomic_text(dest) as handle:
            for paper in items:
                handle.write(
                    json.dumps(
                        paper.to_dict(),
                        ensure_ascii=False,
                        sort_keys=False,
                        separators=(",", ":"),
                    )
                )
                handle.write("\n")
    except OSError as exc:
        raise ExportError(f"could not write {_label(dest)}: {exc}", cause=exc) from exc


def _dump(payload: dict[str, Any], dest: Any, *, indent: int | None) -> None:
    try:
        with _atomic_text(dest) as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=indent,
                sort_keys=False,
            )
            handle.write("\n")
    except OSError as exc:
        raise ExportError(f"could not write {_label(dest)}: {exc}", cause=exc) from exc


# ---------------------------------------------------------------------- #
# Reading
# ---------------------------------------------------------------------- #


def from_json(src: str | os.PathLike[str] | TextIO) -> list[Paper]:
    """Read a JSON envelope written by :func:`to_json`."""
    try:
        with _text(src) as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise ExportError(f"could not read {_label(src)}: {exc}", cause=exc) from exc
    except json.JSONDecodeError as exc:
        raise ExportError(f"invalid JSON in {_label(src)}: {exc}", cause=exc) from exc

    if not isinstance(payload, Mapping):
        raise ExportError(
            f"expected a JSON object at the top level of {_label(src)}, "
            f"got {type(payload).__name__}"
        )

    _check_version(payload)
    raw = _require_papers(payload)
    return [_coerce(entry, f"index {index}") for index, entry in enumerate(raw)]


def from_jsonl(src: str | os.PathLike[str] | TextIO) -> list[Paper]:
    """Read a JSON Lines file written by :func:`to_jsonl`.

    Blank lines are skipped (a hand-edited stream is still readable); every
    other failure names the offending line number.
    """
    papers: list[Paper] = []
    try:
        with _text(src) as handle:
            for lineno, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    entry = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ExportError(
                        f"line {lineno}: invalid JSON ({exc})", cause=exc
                    ) from exc
                papers.append(_coerce(entry, f"line {lineno}"))
    except OSError as exc:
        raise ExportError(f"could not read {_label(src)}: {exc}", cause=exc) from exc
    return papers


def _check_version(payload: Mapping[str, Any]) -> None:
    if "schema_version" not in payload:
        raise ExportError(
            "missing 'schema_version': cannot tell which format this file uses"
        )
    version = payload["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ExportError(f"'schema_version' must be an integer, got {version!r}")
    if version > SCHEMA_VERSION:
        raise ExportError(
            f"file schema_version {version} is newer than the supported "
            f"{SCHEMA_VERSION}; upgrade researcher-python to read it"
        )
    if version < SCHEMA_VERSION:
        # Nothing to migrate yet — version 1 is the only one that has existed.
        pass


def _require_papers(payload: Mapping[str, Any]) -> list[Any]:
    if "papers" not in payload:
        raise ExportError("wrong top-level shape: missing the 'papers' array")
    papers = payload["papers"]
    if not isinstance(papers, list):
        raise ExportError(
            f"wrong top-level shape: 'papers' must be an array, got {type(papers).__name__}"
        )
    return papers


def _coerce(entry: Any, where: str) -> Paper:
    if not isinstance(entry, Mapping):
        raise ExportError(
            f"paper at {where} is not a JSON object: got {type(entry).__name__}"
        )
    try:
        return Paper.from_dict(entry)
    except Exception as exc:  # noqa: BLE001 - reported with its location
        raise ExportError(f"paper at {where} could not be read: {exc}", cause=exc) from exc


# ---------------------------------------------------------------------- #
# I/O helpers
# ---------------------------------------------------------------------- #


def _now_utc() -> str:
    """UTC timestamp in ISO-8601 with a trailing ``Z``."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_path(value: Any) -> bool:
    return isinstance(value, (str, os.PathLike))


def _label(value: Any) -> str:
    return os.fspath(value) if _is_path(value) else "<stream>"


@contextmanager
def _atomic_text(dest: Any) -> Iterator[TextIO]:
    """Yield a writable handle; a path is written to a sibling temp file first."""
    if _is_path(dest):
        path = Path(os.fspath(dest))
        path.parent.mkdir(parents=True, exist_ok=True)
        # mkstemp gives a unique name even when two writers share a directory;
        # a PID suffix would not.
        fd, temp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp"
        )
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                yield handle
            os.replace(temp, path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        return

    if not hasattr(dest, "write"):
        raise ExportError(
            f"dest must be a path or a text file object, got {type(dest).__name__}"
        )
    yield dest


@contextmanager
def _text(src: Any) -> Iterator[TextIO]:
    """Yield a readable handle, closing it only when we opened it."""
    if _is_path(src):
        with open(Path(os.fspath(src)), encoding="utf-8") as handle:
            yield handle
        return

    if not hasattr(src, "read"):
        raise ExportError(
            f"src must be a path or a text file object, got {type(src).__name__}"
        )
    yield src
