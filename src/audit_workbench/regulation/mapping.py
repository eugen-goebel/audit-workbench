"""Rule -> regulation references, read from ``regulation/mapping.yaml``.

The mapping is documentation and data at once: every rule id points to the
provisions it implements, each provision carrying a verbatim quote from the
article text in ``regulation/sources/`` and the official URL. The quote is what
makes a finding defensible, so the tests check that every quote really is a
substring of its source file.

Findings keep the references their rule emits itself and gain the ones from the
mapping that are missing. A rule that already names its provision in its own
wording therefore keeps that wording, and rules without references (the access
and cross-module rules) get the umbrella provision plus the specific paragraph.

A missing mapping file is not an error. An installed wheel has no repository
root, and the workbench must still run; findings then simply carry the
references their rules set themselves. A file that exists but is malformed is an
error, because a silently ignored mapping would hide a broken citation.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, used for typing only
    from audit_workbench.core.findings import Finding

MAPPING_ENV = "AWB_REGULATION_MAPPING"
"""Environment variable with the path of the mapping file, overriding the default."""

MAPPING_PATH = Path(__file__).resolve().parents[3] / "regulation" / "mapping.yaml"
"""Default location: ``<repository root>/regulation/mapping.yaml``."""

_FIELDS = ("framework", "ref", "quote", "url")


class MappingError(ValueError):
    """The mapping file exists but is not a valid rule -> references mapping."""


@dataclass(frozen=True)
class Reference:
    """One cited provision: who published it, which article, the quote and where it lives."""

    framework: str
    ref: str
    quote: str
    url: str

    def __str__(self) -> str:
        return f"{self.framework} {self.ref}"


def _default_path() -> Path:
    override = os.environ.get(MAPPING_ENV)
    return Path(override) if override else MAPPING_PATH


@lru_cache(maxsize=4)
def _read(path: str) -> Mapping[str, tuple[Reference, ...]]:
    file = Path(path)
    if not file.is_file():
        return {}
    raw: Any = yaml.safe_load(file.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise MappingError(f"{file}: expected a mapping of rule id to a list of references")
    mapping: dict[str, tuple[Reference, ...]] = {}
    for rule_id, entries in raw.items():
        if not isinstance(rule_id, str) or not isinstance(entries, list):
            raise MappingError(f"{file}: {rule_id!r} must map to a list of references")
        refs: list[Reference] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise MappingError(f"{file}: {rule_id}[{index}] must be a mapping")
            missing = [field for field in _FIELDS if not str(entry.get(field) or "").strip()]
            if missing:
                raise MappingError(f"{file}: {rule_id}[{index}] lacks {', '.join(missing)}")
            refs.append(
                Reference(
                    framework=str(entry["framework"]).strip(),
                    ref=str(entry["ref"]).strip(),
                    quote=str(entry["quote"]).strip(),
                    url=str(entry["url"]).strip(),
                )
            )
        mapping[rule_id] = tuple(refs)
    return mapping


def load_mapping(path: Path | str | None = None) -> Mapping[str, tuple[Reference, ...]]:
    """The mapping for *path*, defaulting to :data:`MAPPING_PATH`.

    Returns an empty mapping when the file does not exist. The result is cached,
    so repeated calls during a run parse the file once.
    """
    return _read(str(path or _default_path()))


def reference_strings(mapping: Mapping[str, tuple[Reference, ...]] | None = None) -> set[str]:
    """Every reference of the mapping as the display string a finding carries."""
    known = load_mapping() if mapping is None else mapping
    return {str(ref) for refs in known.values() for ref in refs}


def apply_references(
    findings: Iterable[Finding],
    mapping: Mapping[str, tuple[Reference, ...]] | None = None,
) -> list[Finding]:
    """Append the mapped references to each finding, skipping the ones it already carries.

    Findings without a mapping entry and findings that already name every mapped
    provision are returned unchanged. Order is preserved, so the reference a rule
    writes itself stays first.
    """
    refs_by_rule = load_mapping() if mapping is None else mapping
    result: list[Finding] = []
    for finding in findings:
        known: Sequence[str] = tuple(str(ref) for ref in refs_by_rule.get(finding.rule_id, ()))
        missing = [ref for ref in known if ref not in finding.regulation_refs]
        result.append(
            finding.model_copy(update={"regulation_refs": [*finding.regulation_refs, *missing]})
            if missing
            else finding
        )
    return result
