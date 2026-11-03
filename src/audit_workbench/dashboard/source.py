"""Where the dashboard gets its run from: a data folder, or a bank generated from a seed.

Generating the bank writes to a temporary folder that is removed again as soon as the run
is loaded, since the loaded run holds everything in memory.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from audit_workbench.reporting import ModuleName
from audit_workbench.runner import Run, RunError, load_run, read_ground_truth
from audit_workbench.synth import generate_bank

DEFAULT_SEED = 7
MAX_SEED = 2**31 - 1


class InvalidSeedError(ValueError):
    """The seed is not a whole number between 0 and 2,147,483,647."""


class SourceError(RuntimeError):
    """The run could not be made from the chosen source."""


@dataclass(frozen=True)
class Source:
    """A loaded run with where it came from."""

    run: Run
    directory: Path | None
    seed: int | None
    generated: bool
    truth: dict[str, Any] | None


def parse_seed(raw: str | int | None) -> int:
    """A seed from a URL parameter or a number; `None` gives the default seed 7."""
    if raw is None:
        return DEFAULT_SEED
    if isinstance(raw, bool):
        raise InvalidSeedError(f"invalid seed {raw!r}")
    text = str(raw).strip()
    if not text.isascii() or not text.isdigit():
        raise InvalidSeedError(f"invalid seed {raw!r}: use a whole number from 0 to {MAX_SEED}")
    seed = int(text)
    if seed > MAX_SEED:
        raise InvalidSeedError(f"invalid seed {raw!r}: use a whole number from 0 to {MAX_SEED}")
    return seed


def _truth_seed(truth: dict[str, Any] | None) -> int | None:
    seed = truth.get("seed") if truth else None
    return seed if isinstance(seed, int) else None


def load_source(
    directory: Path | None, as_of: date | None, module: ModuleName, seed: int
) -> Source:
    """Run the rules on `directory`, or on a bank generated for `seed` when it is None."""
    try:
        if directory is not None:
            run = load_run(directory, as_of, module)
            truth = read_ground_truth(directory)
            return Source(run, directory, _truth_seed(truth), False, truth)
        with tempfile.TemporaryDirectory(prefix="awb-dashboard-") as tmp:
            bank = generate_bank(Path(tmp) / "bank", seed=seed)
            run = load_run(bank, as_of, module)
            truth = read_ground_truth(bank)
        return Source(run, None, seed, True, truth)
    except (RunError, OSError, ValueError, KeyError) as exc:
        where = str(directory) if directory is not None else f"the generated bank (seed {seed})"
        raise SourceError(f"cannot run {where}: {exc}") from exc
