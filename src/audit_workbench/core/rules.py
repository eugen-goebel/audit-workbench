"""Rule base class and registry.

A rule is one audit test, for example "account still active after the
employee left". Each rule carries an id and a version. Both end up in every
finding, so a result always says which logic produced it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar

from audit_workbench.core.findings import Finding, Severity


class Rule(ABC):
    rule_id: ClassVar[str]
    version: ClassVar[str]
    title: ClassVar[str]

    def __init__(self, **params: Any) -> None:
        for attr in ("rule_id", "version", "title"):
            if not getattr(type(self), attr, None):
                raise TypeError(f"{type(self).__name__} must define class attribute {attr!r}")
        self.params = params

    @abstractmethod
    def run(self, data: Mapping[str, Any]) -> list[Finding]:
        """Run the test. Must not modify the input data."""

    def finding(
        self,
        *,
        severity: Severity,
        entity: str,
        description: str,
        evidence: list[str],
        details: dict[str, Any] | None = None,
        regulation_refs: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Finding:
        """Build a finding. `context` adds run-level inputs such as the audit
        date to the recorded parameters, so the finding id depends on them."""
        return Finding(
            rule_id=self.rule_id,
            rule_version=self.version,
            severity=severity,
            entity=entity,
            description=description,
            evidence=evidence,
            params={**self.params, **(context or {})},
            details=details or {},
            regulation_refs=regulation_refs or [],
        )


class RuleRegistry:
    def __init__(self) -> None:
        self._rules: dict[str, type[Rule]] = {}

    def register(self, rule_cls: type[Rule]) -> type[Rule]:
        if rule_cls.rule_id in self._rules:
            raise ValueError(f"rule id {rule_cls.rule_id!r} is already registered")
        self._rules[rule_cls.rule_id] = rule_cls
        return rule_cls

    def ids(self) -> list[str]:
        return sorted(self._rules)

    def get(self, rule_id: str) -> type[Rule]:
        """The rule class registered under `rule_id`."""
        return self._rules[rule_id]

    def run_all(
        self,
        data: Mapping[str, Any],
        params: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> list[Finding]:
        params = params or {}
        findings: list[Finding] = []
        for rule_id in self.ids():
            rule = self._rules[rule_id](**params.get(rule_id, {}))
            findings.extend(rule.run(data))
        return findings
