"""The finding model shared by all audit modules."""

from __future__ import annotations

import hashlib
import json
from enum import IntEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


class Severity(IntEnum):
    """Ordered so findings can be sorted and filtered by threshold."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


Sha256 = str


class Finding(BaseModel):
    """One audit exception, with everything needed to reproduce it.

    The id is derived from the content (rule, version, entity, parameters,
    evidence). Rerunning the same rule on the same data gives the same id,
    which makes findings comparable between runs.
    """

    model_config = ConfigDict(frozen=True)

    rule_id: str
    rule_version: str
    severity: Severity
    entity: str
    description: str
    evidence: list[Sha256] = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)
    regulation_refs: list[str] = Field(default_factory=list)

    @field_validator("evidence")
    @classmethod
    def _evidence_are_hashes(cls, value: list[str]) -> list[str]:
        bad = [h for h in value if len(h) != 64 or any(c not in "0123456789abcdef" for c in h)]
        if bad:
            raise ValueError(f"evidence must be lowercase SHA-256 hex digests, got {bad!r}")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def finding_id(self) -> str:
        key = json.dumps(
            {
                "rule": self.rule_id,
                "version": self.rule_version,
                "entity": self.entity,
                "params": self.params,
                "evidence": sorted(self.evidence),
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(key.encode()).hexdigest()[:16]
