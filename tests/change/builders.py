"""A small builder for hand-made change management datasets in tests.

Each rule test describes only the changes it cares about. The builder fills
in defaults, writes the CSV files of the change schema and loads them, so
tests run against the same loading path as production code.

    data = MiniChanges()
    cid = data.change()                       # normal change, approved, tested
    data.deploy(cid, deployed_ts="2026-03-02T20:00:00")
    loaded = data.load(tmp_path)

Default cast (all active employees):

- ``DEV1``, ``DEV2``: developers (Entwicklung), AD accounts ``AD-DEV1``/``AD-DEV2``
- ``OPS1``: operator (IT-Betrieb/Administration), AD account ``AD-OPS1``
- ``REL1``: release manager (IT-Betrieb/Sachbearbeitung)
- ``CAB1``: approver (IT-Betrieb/Teamleitung)
- ``REQ1``: requester (Zahlungsverkehr/Teamleitung)
- ``TST1``: tester (Zahlungsverkehr/Analyse)
- ``AD-PIPE``: technical pipeline account
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from audit_workbench.change.data import SCHEMA, ChangeData, load_change_evidence

AS_OF = date(2026, 9, 30)

_CAST = [
    ("DEV1", "Entwicklung", "Sachbearbeitung"),
    ("DEV2", "Entwicklung", "Sachbearbeitung"),
    ("OPS1", "IT-Betrieb", "Administration"),
    ("REL1", "IT-Betrieb", "Sachbearbeitung"),
    ("CAB1", "IT-Betrieb", "Teamleitung"),
    ("REQ1", "Zahlungsverkehr", "Teamleitung"),
    ("TST1", "Zahlungsverkehr", "Analyse"),
]


class MiniChanges:
    def __init__(self, as_of: date = AS_OF) -> None:
        self.as_of = as_of
        self.rows: dict[str, list[dict[str, str]]] = {name: [] for name in SCHEMA}
        self._n: dict[str, int] = {}
        for emp, unit, func in _CAST:
            self.employee(emp, org_unit=unit, job_function=func)
        for emp in ("DEV1", "DEV2", "OPS1", "REL1", "CAB1", "REQ1", "TST1"):
            self.account(f"AD-{emp}", emp)
        self.account("AD-PIPE")

    def _next(self, prefix: str) -> str:
        self._n[prefix] = self._n.get(prefix, 0) + 1
        return f"{prefix}-{self._n[prefix]:05d}"

    # --- master data ----------------------------------------------------

    def employee(
        self,
        employee_id: str,
        *,
        org_unit: str = "Entwicklung",
        job_function: str = "Sachbearbeitung",
        exit_date: str = "",
    ) -> MiniChanges:
        self.rows["hr_employees"].append(
            dict(
                employee_id=employee_id,
                name=f"Person {employee_id}",
                org_unit=org_unit,
                job_function=job_function,
                entry_date="2020-01-01",
                exit_date=exit_date,
            )
        )
        return self

    def account(
        self,
        account_id: str,
        employee_id: str = "",
        *,
        system: str = "AD",
        created_at: str = "2020-01-01T08:00:00",
    ) -> MiniChanges:
        """A personal account, or a technical one if `employee_id` is empty."""
        self.rows["accounts"].append(
            dict(
                account_id=account_id,
                system=system,
                employee_id=employee_id,
                account_type="personal" if employee_id else "technical",
                interactive="true" if employee_id else "false",
                status="active",
                created_at=created_at,
                last_login="2026-09-25T09:00:00",
            )
        )
        return self

    # --- changes --------------------------------------------------------

    def change(
        self,
        change_id: str | None = None,
        *,
        system: str = "PAY",
        change_type: str = "normal",
        risk: str = "medium",
        requester: str = "REQ1",
        created_ts: str = "2026-03-01T09:00:00",
        test_signoff_ts: str | None = "2026-03-01T15:00:00",
        tester: str = "TST1",
        status: str = "closed",
        approver: str | None = "CAB1",
        approved_ts: str = "2026-03-01T16:00:00",
        author: str | None = "DEV1",
        committed_ts: str = "2026-03-01T11:00:00",
    ) -> str:
        """Add one change. Returns the change_id.

        By default the change is approved (`pre`) by CAB1, tested by TST1 and
        has one commit by DEV1. Pass ``approver=None`` / ``author=None`` /
        ``test_signoff_ts=None`` to leave the approval / commit / sign-off out.
        """
        change_id = change_id or self._next("CHG")
        self.rows["changes"].append(
            dict(
                change_id=change_id,
                system=system,
                change_type=change_type,
                risk=risk,
                title=f"Change {change_id}",
                requester_employee_id=requester,
                created_ts=created_ts,
                test_signoff_ts=test_signoff_ts or "",
                tester_employee_id=tester if test_signoff_ts else "",
                status=status,
            )
        )
        if approver is not None:
            self.approval(change_id, approver=approver, decided_ts=approved_ts)
        if author is not None:
            self.commit(change_id, author=author, committed_ts=committed_ts, system=system)
        return change_id

    def approval(
        self,
        change_id: str,
        *,
        approver: str = "CAB1",
        decided_ts: str = "2026-03-01T16:00:00",
        stage: str = "pre",
        decision: str = "approved",
    ) -> MiniChanges:
        self.rows["change_approvals"].append(
            dict(
                change_id=change_id,
                approval_stage=stage,
                approver_employee_id=approver,
                decision=decision,
                decided_ts=decided_ts,
            )
        )
        return self

    def commit(
        self,
        change_id: str,
        *,
        author: str = "DEV1",
        committed_ts: str = "2026-03-01T11:00:00",
        system: str = "PAY",
        sha: str | None = None,
    ) -> str:
        sha = sha or f"{len(self.rows['commits']) + 1:040x}"
        self.rows["commits"].append(
            dict(
                commit_sha=sha,
                system=system,
                author_employee_id=author,
                committed_ts=committed_ts,
                change_id=change_id,
            )
        )
        return sha

    def deploy(
        self,
        change_id: str = "",
        *,
        deployed_ts: str = "2026-03-02T20:00:00",
        system: str = "PAY",
        environment: str = "prod",
        method: str = "manual",
        account_id: str | None = None,
        released_by: str = "REL1",
        result: str = "success",
        deployment_id: str | None = None,
    ) -> str:
        """Add one deployment. Returns the deployment_id.

        Manual deployments run under ``AD-OPS1`` by default, pipeline
        deployments under ``AD-PIPE``, released by ``released_by``.
        """
        deployment_id = deployment_id or self._next("DEP")
        if account_id is None:
            account_id = "AD-OPS1" if method == "manual" else "AD-PIPE"
        self.rows["deployments"].append(
            dict(
                deployment_id=deployment_id,
                system=system,
                environment=environment,
                deployed_ts=deployed_ts,
                account_id=account_id,
                method=method,
                released_by_employee_id=released_by if method == "pipeline" else "",
                change_id=change_id,
                result=result,
            )
        )
        return deployment_id

    # --- output ---------------------------------------------------------

    def write(self, directory: Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        for name, columns in SCHEMA.items():
            with (directory / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
                writer.writeheader()
                writer.writerows(self.rows[name])
        return directory

    def load(self, directory: Path) -> ChangeData:
        return ChangeData(evidence=load_change_evidence(self.write(directory)), as_of=self.as_of)
