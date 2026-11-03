"""Cross-module chains: X findings with their linking key and the findings behind them."""

from __future__ import annotations

from audit_workbench.dashboard import views
from audit_workbench.runner import Run


def _chain(run: Run, rule_id: str, index: int = 0):
    return [c for c in views.cross_chains(run) if c.finding.rule_id == rule_id][index]


def test_one_chain_per_x_finding(run7: Run) -> None:
    chains = views.cross_chains(run7)
    assert len(chains) == 8
    assert [c.finding.rule_id for c in chains].count("X-01") == 2
    assert all(c.title and c.link_key and c.steps for c in chains)


def test_related_findings_exist_in_the_run(run7: Run) -> None:
    ids = {f.finding_id for f in run7.findings}
    for chain in views.cross_chains(run7):
        assert chain.related
        assert {f.finding_id for f in chain.related} <= ids
        assert {(f.rule_id, f.entity) for f in chain.related} == {
            tuple(pair) for pair in chain.finding.details["related"]
        }


def test_x01_chain_runs_from_the_conflict_to_the_journal_entries(run7: Run) -> None:
    chain = _chain(run7, "X-01")
    employee, conflict = chain.finding.entity.split(":")
    assert employee in chain.link_key and conflict in chain.link_key
    assert chain.steps[0].module == "A" and chain.steps[0].rule_id == "A-05"
    assert chain.steps[0].finding_id in {f.finding_id for f in run7.findings}
    entries = [s for s in chain.steps if s.kind == "journal entry"]
    assert [s.key for s in entries] == chain.finding.details["doc_nos"]
    assert all(s.module == "B" for s in entries)
    assert chain.modules == ["A", "B"]


def test_x02_chain_names_the_account_and_the_function_it_did_not_hold(run7: Run) -> None:
    chain = _chain(run7, "X-02")
    check = chain.finding.details["checks"][0]
    assert check["account_id"] in chain.link_key
    text = " ".join(s.label for s in chain.steps)
    assert check["function"] in text and check["account_id"] in text
    assert "B" in chain.modules and "A" in chain.modules


def test_x03_chain_shows_the_exit_date_and_the_activity_after_it(run7: Run) -> None:
    chain = _chain(run7, "X-03")
    details = chain.finding.details
    assert chain.finding.entity in chain.link_key
    assert details["exit_date"] in chain.steps[0].label
    actions = [s for s in chain.steps if s.kind == "action"]
    assert [s.key for s in actions] == [a["id"] for a in details["actions"]]
    assert all(s.module == "C" for s in actions)  # commits, deployments, approvals
    assert "C" in chain.modules
