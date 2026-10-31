"""Tests for the finding model and the rule registry."""

import pandas as pd
import pytest
from pydantic import ValidationError

from audit_workbench.core.findings import Finding, Severity
from audit_workbench.core.rules import Rule, RuleRegistry


def make_finding(**kw):
    base = dict(
        rule_id="A-01",
        rule_version="1.0.0",
        severity=Severity.HIGH,
        entity="U42",
        description="Account active after exit date",
        evidence=["a" * 64],
        params={"grace_days": 0},
    )
    base.update(kw)
    return Finding(**base)


def test_finding_id_is_stable_for_same_content():
    assert make_finding().finding_id == make_finding().finding_id


def test_finding_id_changes_with_entity():
    assert make_finding().finding_id != make_finding(entity="U43").finding_id


def test_finding_id_changes_with_params():
    assert make_finding().finding_id != make_finding(params={"grace_days": 5}).finding_id


def test_evidence_must_be_sha256_hex():
    with pytest.raises(ValidationError):
        make_finding(evidence=["not-a-hash"])


def test_finding_is_immutable():
    f = make_finding()
    with pytest.raises(ValidationError):
        f.entity = "other"


def test_severity_order():
    assert Severity.LOW < Severity.MEDIUM < Severity.HIGH < Severity.CRITICAL


class _AlwaysOne(Rule):
    rule_id = "T-01"
    version = "1.0.0"
    title = "Test rule"

    def run(self, data):
        return [
            self.finding(
                severity=Severity.LOW,
                entity="X",
                description="test",
                evidence=["b" * 64],
            )
        ]


def test_rule_helper_fills_id_version_and_params():
    rule = _AlwaysOne(threshold=3)
    (f,) = rule.run({})
    assert f.rule_id == "T-01"
    assert f.rule_version == "1.0.0"
    assert f.params == {"threshold": 3}


def test_registry_rejects_duplicate_ids():
    reg = RuleRegistry()
    reg.register(_AlwaysOne)
    with pytest.raises(ValueError, match="T-01"):
        reg.register(_AlwaysOne)


def test_registry_runs_all_rules_and_does_not_mutate_input():
    reg = RuleRegistry()
    reg.register(_AlwaysOne)
    frame = pd.DataFrame({"a": [1, 2]})
    before = frame.copy()
    findings = reg.run_all({"t": frame})
    assert [f.rule_id for f in findings] == ["T-01"]
    pd.testing.assert_frame_equal(frame, before)


def test_rule_subclass_must_define_metadata():
    class Broken(Rule):
        def run(self, data):
            return []

    with pytest.raises(TypeError, match="rule_id"):
        Broken()
