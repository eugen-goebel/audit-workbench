"""Load input files as evidence: validated, hashed and timestamped.

Every audit result has to be traceable to the exact data it came from. The
SHA-256 hash of each input file is stored with every finding, so a reviewer
can confirm they are rerunning a test on byte-identical data.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

_CHUNK = 1 << 20
_READERS = {
    ".csv": lambda p: pd.read_csv(p, dtype=str, keep_default_na=False),
    ".parquet": pd.read_parquet,
}


class EvidenceError(ValueError):
    """Raised when an input file cannot be used as audit evidence."""


def sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file, read in chunks."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Evidence:
    """One input file with the metadata needed to reproduce a test."""

    name: str
    path: Path
    sha256: str
    rows: int
    loaded_at: datetime
    frame: pd.DataFrame = field(repr=False, compare=False)


def load_evidence(path: Path, *, name: str, required_columns: Iterable[str]) -> Evidence:
    """Read a CSV or Parquet file, check its columns and hash it.

    CSV values are read as text. Identifiers such as "007" must stay
    strings, otherwise joins between systems fail without any error.
    """
    path = Path(path)
    if not path.is_file():
        raise EvidenceError(f"{name}: file not found: {path}")
    reader = _READERS.get(path.suffix.lower())
    if reader is None:
        raise EvidenceError(
            f"{name}: unsupported format {path.suffix!r}, expected one of {sorted(_READERS)}"
        )

    # Hash before reading, so the hash describes exactly the bytes that were parsed.
    digest = sha256_file(path)
    frame = reader(path)

    missing = [c for c in required_columns if c not in frame.columns]
    if missing:
        raise EvidenceError(f"{name}: missing required columns: {', '.join(missing)}")

    return Evidence(
        name=name,
        path=path,
        sha256=digest,
        rows=len(frame),
        loaded_at=datetime.now(UTC),
        frame=frame,
    )
