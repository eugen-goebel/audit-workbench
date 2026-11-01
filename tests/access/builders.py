"""A small builder for hand-made access datasets in tests.

Each rule test describes only the rows it cares about. The builder fills in
defaults, writes the CSV files of the access schema and loads them, so tests
run against the same loading path as production code.

    bank = MiniBank(as_of=date(2026, 9, 30))
    bank.employee("E1", exit_date="2026-08-01")
    bank.account("PAY-1", employee_id="E1")
    data = bank.load(tmp_path)
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from audit_workbench.access.data import SCHEMA, AccessData, load_access_evidence


class MiniBank:
    def __init__(self, as_of: date = date(2026, 9, 30)) -> None:
        self.as_of = as_of
        self.rows: dict[str, list[dict[str, str]]] = {name: [] for name in SCHEMA}

    def employee(
        self,
        employee_id: str,
        *,
        org_unit: str = "Zahlungsverkehr",
        job_function: str = "Sachbearbeitung",
        entry_date: str = "2020-01-01",
        exit_date: str = "",
        name: str | None = None,
    ) -> MiniBank:
        self.rows["hr_employees"].append(
            dict(
                employee_id=employee_id,
                name=name or f"Person {employee_id}",
                org_unit=org_unit,
                job_function=job_function,
                entry_date=entry_date,
                exit_date=exit_date,
            )
        )
        return self

    def account(
        self,
        account_id: str,
        *,
        employee_id: str = "",
        system: str = "PAY",
        account_type: str = "personal",
        interactive: bool = True,
        status: str = "active",
        created_at: str = "2020-01-01T08:00:00",
        last_login: str = "2026-09-29T09:00:00",
    ) -> MiniBank:
        self.rows["accounts"].append(
            dict(
                account_id=account_id,
                system=system,
                employee_id=employee_id,
                account_type=account_type,
                interactive="true" if interactive else "false",
                status=status,
                created_at=created_at,
                last_login=last_login,
            )
        )
        return self

    def role(
        self, role_id: str, *, system: str = "PAY", privileged: bool = False, name: str = ""
    ) -> MiniBank:
        self.rows["roles"].append(
            dict(
                role_id=role_id,
                system=system,
                name=name or role_id,
                privileged="true" if privileged else "false",
            )
        )
        return self

    def contains(self, parent_role_id: str, child_role_id: str) -> MiniBank:
        self.rows["role_hierarchy"].append(
            dict(parent_role_id=parent_role_id, child_role_id=child_role_id)
        )
        return self

    def entitlement(
        self, entitlement_id: str, business_function: str, *, system: str = "PAY"
    ) -> MiniBank:
        self.rows["entitlements"].append(
            dict(
                entitlement_id=entitlement_id,
                system=system,
                name=entitlement_id,
                business_function=business_function,
            )
        )
        return self

    def grants(self, role_id: str, entitlement_id: str) -> MiniBank:
        self.rows["role_entitlements"].append(dict(role_id=role_id, entitlement_id=entitlement_id))
        return self

    def assign(
        self, account_id: str, role_id: str, *, valid_from: str = "2020-01-01", valid_to: str = ""
    ) -> MiniBank:
        self.rows["account_roles"].append(
            dict(account_id=account_id, role_id=role_id, valid_from=valid_from, valid_to=valid_to)
        )
        return self

    def campaign(
        self,
        campaign_id: str,
        *,
        system: str = "PAY",
        start_date: str = "2026-03-01",
        end_date: str = "2026-03-31",
    ) -> MiniBank:
        self.rows["recert_campaigns"].append(
            dict(campaign_id=campaign_id, system=system, start_date=start_date, end_date=end_date)
        )
        return self

    def decision(
        self,
        campaign_id: str,
        account_id: str,
        role_id: str,
        reviewer_employee_id: str,
        *,
        decision: str = "keep",
        item_shown_at: str = "2026-03-10T10:00:00",
        decided_at: str = "2026-03-10T10:01:00",
    ) -> MiniBank:
        self.rows["recert_decisions"].append(
            dict(
                campaign_id=campaign_id,
                account_id=account_id,
                role_id=role_id,
                reviewer_employee_id=reviewer_employee_id,
                decision=decision,
                item_shown_at=item_shown_at,
                decided_at=decided_at,
            )
        )
        return self

    def write(self, directory: Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        for name, columns in SCHEMA.items():
            with (directory / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
                writer.writeheader()
                writer.writerows(self.rows[name])
        return directory

    def load(self, directory: Path) -> AccessData:
        return AccessData(evidence=load_access_evidence(self.write(directory)), as_of=self.as_of)
