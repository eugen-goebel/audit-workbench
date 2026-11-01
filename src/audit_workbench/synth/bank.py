"""Deterministic generator for the synthetic test bank "Musterbank AG".

``generate_bank(out_dir, seed=7, inject=True)`` writes all nine CSV files of
the access schema (exact column order from ``access.data.SCHEMA``), the five
journal files of module B (``gl_accounts.csv``, ``journal_entries.csv``,
``journal_lines.csv``, ``close_calendar.csv``, ``approval_limits.csv``) plus
``ground_truth.json``. The journal ledger is built by
``audit_workbench.synth.ledger`` from its own random stream
(``default_rng([seed, 2])``), so the module A files stay byte-identical. With the same seed the output is byte-identical; only
``numpy.random.default_rng(seed)`` is used, never the global ``random``
module or the wall clock.

The clean bank (``inject=False``) triggers none of the rules A-01 to A-08.
``inject_issues`` (see ``inject.py``) then adds exactly the issue counts of
spec section 5 and records every injected issue in ``ground_truth.json``.

Structure of the clean bank
---------------------------

* ~820 employees in six org units (plus Revision), several job functions per
  unit, one personal account per employee in their unit's system, plus a few
  technical and generic accounts.
* Roles are mapped uniformly per (org_unit, job_function), so peer groups
  hold their standard role and rule A-08 stays quiet. Normal job profiles
  never combine conflicting business functions.
* One recertification campaign per system inside the 12 months before
  ``as_of``; every privileged assignment on an active account of a
  non-leaver has a ``keep`` decision with a realistic 20-300 s decision time,
  and no reviewer ever reviews their own account.

Near misses (spec section 5.4) and where each one is created in this module
---------------------------------------------------------------------------

1. Leaver whose account is disabled           -- ``_add_clean_leavers``
2. Leaver inside the grace period (exit_date == as_of, active account)
                                              -- ``_add_near_misses``
3. Account last used exactly 89 days before as_of
                                              -- ``_add_near_misses``
4. Non-interactive generic account            -- ``_add_shared_accounts``
5. Technical account that never logged in     -- ``_add_shared_accounts``
6. Privileged role reviewed in a campaign that ended 300 days before as_of
                                              -- campaign CAM-CORE end date
7. Employee with two functions that do not conflict (PAYMENT_CREATE +
   BENEFICIARY_MAINTAIN)                      -- ``_add_near_misses``
8. Reviewer with many decisions that are slow enough (>= 20 decisions, all
   >= 5 s, no self-review)                    -- every campaign reviewer
9. Rare role in a peer group below ``min_peers`` (a Sonderaufgaben member
   additionally holds R-TRD-TRADER; the group has 4 members)
                                              -- ``_add_near_misses``
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from audit_workbench.access.data import SCHEMA

AS_OF = date(2026, 9, 30)

FIRST_NAMES = [
    "Anna",
    "Bernd",
    "Clara",
    "Dieter",
    "Eva",
    "Franz",
    "Greta",
    "Hans",
    "Ines",
    "Jonas",
    "Katrin",
    "Lukas",
    "Marta",
    "Nils",
    "Otto",
    "Petra",
    "Quirin",
    "Ruth",
    "Stefan",
    "Tina",
    "Ulf",
    "Vera",
    "Werner",
    "Xenia",
    "Yusuf",
    "Zoe",
    "Anton",
    "Birgit",
    "Christian",
    "Doris",
    "Emil",
    "Frida",
]

LAST_NAMES = [
    "Ackermann",
    "Bauer",
    "Brandt",
    "Dietrich",
    "Ebert",
    "Fischer",
    "Gerber",
    "Hartmann",
    "Ibrahim",
    "Jensen",
    "Keller",
    "Lindner",
    "Meyer",
    "Neumann",
    "Ortner",
    "Peters",
    "Quandt",
    "Richter",
    "Schneider",
    "Thaler",
    "Unger",
    "Vogel",
    "Wagner",
    "Xander",
    "Zimmermann",
    "Albrecht",
    "Berger",
    "Czak",
    "Denzler",
    "Egli",
    "Furrer",
    "Graf",
    "Huber",
    "Ivanov",
    "Jost",
    "Krause",
    "Lehmann",
    "Moser",
    "Nagy",
    "Steiner",
]

ENTITLEMENTS: dict[str, tuple[str, str, str]] = {
    "E-PAY-VIEW": ("PAY", "Zahlungen anzeigen", "PAYMENT_VIEW"),
    "E-PAY-CREATE": ("PAY", "Zahlungen erfassen", "PAYMENT_CREATE"),
    "E-PAY-APPROVE": ("PAY", "Zahlungen freigeben", "PAYMENT_APPROVE"),
    "E-PAY-BENE": ("PAY", "Zahlungsempfaenger pflegen", "BENEFICIARY_MAINTAIN"),
    "E-CRD-VIEW": ("CORE", "Kreditakten anzeigen", "CREDIT_VIEW"),
    "E-CRD-ENTER": ("CORE", "Kredite erfassen", "CREDIT_ENTER"),
    "E-CRD-APPROVE": ("CORE", "Kredite freigeben", "CREDIT_APPROVE"),
    "E-CRD-LIMIT": ("CORE", "Kreditlimite pflegen", "LIMIT_MAINTAIN"),
    "E-GL-VIEW": ("GL", "Hauptbuch anzeigen", "GL_VIEW"),
    "E-GL-REPORT": ("GL", "Berichte anzeigen", "REPORT_VIEW"),
    "E-GL-POST": ("GL", "Buchungen erfassen", "GL_POST"),
    "E-GL-APPROVE": ("GL", "Buchungen freigeben", "GL_APPROVE"),
    "E-TRD-VIEW": ("TRADE", "Handelsbestand anzeigen", "TRADE_VIEW"),
    "E-TRD-EXEC": ("TRADE", "Trades ausfuehren", "TRADE_EXECUTE"),
    "E-TRD-SETTLE": ("TRADE", "Trades abwickeln", "TRADE_SETTLE"),
    "E-AD-LOGIN": ("AD", "Verzeichnis anmelden", "AD_LOGIN"),
    "E-AD-USERADMIN": ("AD", "Benutzer verwalten", "USER_ADMIN"),
}

# role_id -> (system, name, privileged, entitlement ids)
ROLES: dict[str, tuple[str, str, bool, tuple[str, ...]]] = {
    "R-PAY-VIEW": ("PAY", "Zahlungen lesen", False, ("E-PAY-VIEW",)),
    "R-PAY-CLERK": ("PAY", "Zahlungssachbearbeit", False, ("E-PAY-CREATE", "E-PAY-VIEW")),
    "R-PAY-TEAMLEAD": ("PAY", "Zahlungsfreigabe", True, ("E-PAY-APPROVE",)),
    "R-PAY-BENE": ("PAY", "Empfaengerpflege", False, ("E-PAY-BENE",)),
    "R-PAY-SUPERVISOR": ("PAY", "Zahlungssupervision", False, ()),
    "R-CRD-VIEW": ("CORE", "Kreditakte lesen", False, ("E-CRD-VIEW",)),
    "R-CRD-CLERK": ("CORE", "Kreditssachbearbeit", False, ("E-CRD-ENTER", "E-CRD-VIEW")),
    "R-CRD-APPROVER": ("CORE", "Kreditfreigabe", True, ("E-CRD-APPROVE",)),
    "R-CRD-LIMIT": ("CORE", "Limitverwalter", True, ("E-CRD-LIMIT",)),
    "R-CRD-SUPERVISOR": ("CORE", "Kreditsupervision", False, ()),
    "R-GL-VIEW": ("GL", "Hauptbuch lesen", False, ("E-GL-VIEW", "E-GL-REPORT")),
    "R-GL-CLERK": ("GL", "Buchhaltungssachbearbeit", False, ("E-GL-POST", "E-GL-VIEW")),
    "R-GL-APPROVER": ("GL", "Buchungsfreigabe", True, ("E-GL-APPROVE",)),
    "R-GL-TEAMLEAD": ("GL", "Buchhaltungsteam", False, ("E-GL-VIEW",)),
    "R-GL-MANAGER": ("GL", "Buchhaltungsleitung", False, ()),
    "R-TRD-VIEW": ("TRADE", "Handelsdaten lesen", False, ("E-TRD-VIEW",)),
    "R-TRD-TRADER": ("TRADE", "Trader", False, ("E-TRD-EXEC", "E-TRD-VIEW")),
    "R-TRD-SETTLER": ("TRADE", "Abwicklung", False, ("E-TRD-SETTLE", "E-TRD-VIEW")),
    "R-TRD-LEAD": ("TRADE", "Handelsleitung", True, ("E-TRD-VIEW",)),
    "R-AD-BASE": ("AD", "Verzeichniszugang", False, ("E-AD-LOGIN",)),
    "R-AD-RO": ("AD", "Verzeichnis lesen", False, ("E-AD-LOGIN",)),
    "R-AD-ADMIN": ("AD", "Verzeichnisadministration", True, ("E-AD-USERADMIN",)),
}

# The GL chain R-GL-MANAGER -> R-GL-TEAMLEAD -> R-GL-CLERK is the required
# three-level inheritance chain. Clean holders (Rechnungswesen/Analyse)
# reach GL_POST through three role levels but never GL_APPROVE.
HIERARCHY: tuple[tuple[str, str], ...] = (
    ("R-PAY-SUPERVISOR", "R-PAY-TEAMLEAD"),
    ("R-CRD-SUPERVISOR", "R-CRD-APPROVER"),
    ("R-GL-MANAGER", "R-GL-TEAMLEAD"),
    ("R-GL-TEAMLEAD", "R-GL-CLERK"),
)

# unit -> (system, {job_function: head count})
UNIT_PLAN: list[tuple[str, str, dict[str, int]]] = [
    (
        "Zahlungsverkehr",
        "PAY",
        {
            "Sachbearbeitung": 100,
            "Teamleitung": 12,
            "Analyse": 30,
            "Administration": 18,
            "Sonderaufgaben": 4,
        },
    ),
    (
        "Kredit",
        "CORE",
        {
            "Sachbearbeitung": 90,
            "Teamleitung": 14,
            "Analyse": 36,
            "Administration": 20,
            "Sonderaufgaben": 4,
        },
    ),
    (
        "Rechnungswesen",
        "GL",
        {
            "Sachbearbeitung": 80,
            "Teamleitung": 10,
            "Analyse": 30,
            "Administration": 20,
            "Sonderaufgaben": 5,
        },
    ),
    (
        "Handel",
        "TRADE",
        {
            "Sachbearbeitung": 60,
            "Teamleitung": 8,
            "Analyse": 34,
            "Administration": 18,
            "Sonderaufgaben": 4,
        },
    ),
    (
        "IT-Betrieb",
        "AD",
        {
            "Sachbearbeitung": 30,
            "Teamleitung": 15,
            "Analyse": 25,
            "Administration": 20,
            "Sonderaufgaben": 4,
        },
    ),
    (
        "Entwicklung",
        "AD",
        {
            "Sachbearbeitung": 60,
            "Analyse": 25,
            "Administration": 15,
        },
    ),
    ("Revision", "GL", {"Analyse": 20, "Teamleitung": 10}),
]

# (org_unit, job_function) -> standard role. Uniform per peer group, so
# rule A-08 finds no rare roles in the clean bank.
ROLE_MAP: dict[tuple[str, str], str] = {
    ("Zahlungsverkehr", "Sachbearbeitung"): "R-PAY-CLERK",
    ("Zahlungsverkehr", "Teamleitung"): "R-PAY-TEAMLEAD",
    ("Zahlungsverkehr", "Analyse"): "R-PAY-BENE",
    ("Zahlungsverkehr", "Administration"): "R-PAY-VIEW",
    ("Zahlungsverkehr", "Sonderaufgaben"): "R-PAY-VIEW",
    ("Kredit", "Sachbearbeitung"): "R-CRD-CLERK",
    ("Kredit", "Teamleitung"): "R-CRD-APPROVER",
    ("Kredit", "Analyse"): "R-CRD-LIMIT",
    ("Kredit", "Administration"): "R-CRD-VIEW",
    ("Kredit", "Sonderaufgaben"): "R-CRD-VIEW",
    ("Rechnungswesen", "Sachbearbeitung"): "R-GL-CLERK",
    ("Rechnungswesen", "Teamleitung"): "R-GL-APPROVER",
    ("Rechnungswesen", "Analyse"): "R-GL-MANAGER",
    ("Rechnungswesen", "Administration"): "R-GL-VIEW",
    ("Rechnungswesen", "Sonderaufgaben"): "R-GL-VIEW",
    ("Handel", "Sachbearbeitung"): "R-TRD-SETTLER",
    ("Handel", "Teamleitung"): "R-TRD-LEAD",
    ("Handel", "Analyse"): "R-TRD-TRADER",
    ("Handel", "Administration"): "R-TRD-VIEW",
    ("Handel", "Sonderaufgaben"): "R-TRD-VIEW",
    ("IT-Betrieb", "Sachbearbeitung"): "R-AD-BASE",
    ("IT-Betrieb", "Teamleitung"): "R-AD-BASE",
    ("IT-Betrieb", "Analyse"): "R-AD-BASE",
    ("IT-Betrieb", "Administration"): "R-AD-ADMIN",
    ("IT-Betrieb", "Sonderaufgaben"): "R-AD-BASE",
    ("Entwicklung", "Sachbearbeitung"): "R-AD-BASE",
    ("Entwicklung", "Analyse"): "R-AD-BASE",
    ("Entwicklung", "Administration"): "R-AD-BASE",
    ("Revision", "Analyse"): "R-GL-VIEW",
    ("Revision", "Teamleitung"): "R-GL-VIEW",
}

SYSTEMS = ["CORE", "PAY", "GL", "TRADE", "AD"]

# campaign_id -> (system, start offset days before as_of, end offset)
# CAM-CORE ends exactly 300 days before as_of (near miss 6).
CAMPAIGN_PLAN: list[tuple[str, str, int, int]] = [
    ("CAM-CORE", "CORE", 330, 300),
    ("CAM-PAY", "PAY", 212, 173),
    ("CAM-GL", "GL", 149, 110),
    ("CAM-TRADE", "TRADE", 121, 82),
    ("CAM-AD", "AD", 91, 47),
]

REVIEWER_SOURCE: dict[str, tuple[str, str]] = {
    "CORE": ("Kredit", "Teamleitung"),
    "PAY": ("Zahlungsverkehr", "Teamleitung"),
    "GL": ("Rechnungswesen", "Teamleitung"),
    "TRADE": ("Handel", "Teamleitung"),
    "AD": ("IT-Betrieb", "Administration"),
}

TECHNICAL_PLAN: dict[str, int] = {"CORE": 3, "PAY": 3, "GL": 2, "TRADE": 2, "AD": 2}


def _ts(day: date, hour: int, minute: int, second: int = 0) -> str:
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}:{second:02d}"


class Bank:
    """Accumulates the rows of the access schema plus lookup indexes."""

    def __init__(self, as_of: date, rng: np.random.Generator) -> None:
        self.as_of = as_of
        self.rng = rng
        self.rows: dict[str, list[dict[str, str]]] = {name: [] for name in SCHEMA}
        self.emp_meta: dict[str, dict[str, Any]] = {}
        self.acct_meta: dict[str, dict[str, Any]] = {}
        self.assignments: dict[str, list[str]] = {}
        self.groups: dict[tuple[str, str], list[str]] = {}
        self.campaign_by_system: dict[str, dict[str, str]] = {}
        self.reviewer_pool: dict[str, list[str]] = {}
        self.used_employees: set[str] = set()
        self._counters: dict[str, int] = {}

    # -- employees -------------------------------------------------------

    def add_employee(self, unit: str, job_function: str, *, exit_date: date | None = None) -> str:
        n = len(self.emp_meta) + 1
        emp_id = f"E{n:05d}"
        system = dict((u, s) for u, s, _ in UNIT_PLAN)[unit]
        first = str(self.rng.choice(FIRST_NAMES))
        last = str(self.rng.choice(LAST_NAMES))
        entry = date(2010, 1, 1) + timedelta(days=int(self.rng.integers(0, 5600)))
        self.emp_meta[emp_id] = {
            "unit": unit,
            "job_function": job_function,
            "system": system,
            "exit_date": exit_date,
        }
        self.rows["hr_employees"].append(
            {
                "employee_id": emp_id,
                "name": f"{first} {last}",
                "org_unit": unit,
                "job_function": job_function,
                "entry_date": entry.isoformat(),
                "exit_date": exit_date.isoformat() if exit_date else "",
            }
        )
        self.groups.setdefault((unit, job_function), []).append(emp_id)
        return emp_id

    def has_left(self, emp_id: str) -> bool:
        exit_date = self.emp_meta[emp_id]["exit_date"]
        return exit_date is not None and exit_date < self.as_of

    # -- accounts --------------------------------------------------------

    def next_account_id(self, system: str) -> str:
        self._counters[system] = self._counters.get(system, 0) + 1
        return f"{system}-{self._counters[system]:05d}"

    def add_account(
        self,
        system: str,
        *,
        employee_id: str = "",
        account_type: str = "personal",
        interactive: bool = True,
        status: str = "active",
        created_at: str = "",
        last_login: str = "",
    ) -> str:
        account_id = self.next_account_id(system)
        self.acct_meta[account_id] = {"employee_id": employee_id, "system": system}
        self.rows["accounts"].append(
            {
                "account_id": account_id,
                "system": system,
                "employee_id": employee_id,
                "account_type": account_type,
                "interactive": "true" if interactive else "false",
                "status": status,
                "created_at": created_at,
                "last_login": last_login,
            }
        )
        self.assignments.setdefault(account_id, [])
        return account_id

    def personal_login(self, *, max_days: int = 60) -> str:
        """A deterministic recent login timestamp for an active account."""
        days = int(self.rng.integers(1, max_days + 1))
        return _ts(
            self.as_of - timedelta(days=days),
            int(self.rng.integers(7, 19)),
            int(self.rng.integers(0, 60)),
        )

    def assign(self, account_id: str, role_id: str, *, valid_from: str = "2020-01-01") -> None:
        self.assignments.setdefault(account_id, []).append(role_id)
        self.rows["account_roles"].append(
            {
                "account_id": account_id,
                "role_id": role_id,
                "valid_from": valid_from,
                "valid_to": "",
            }
        )

    def roles_of(self, account_id: str) -> list[str]:
        return list(self.assignments.get(account_id, []))

    # -- recertification -------------------------------------------------

    def add_decision(
        self,
        campaign_id: str,
        account_id: str,
        role_id: str,
        reviewer: str,
        *,
        decision: str = "keep",
        item_shown_at: str,
        decided_at: str,
    ) -> None:
        self.rows["recert_decisions"].append(
            {
                "campaign_id": campaign_id,
                "account_id": account_id,
                "role_id": role_id,
                "reviewer_employee_id": reviewer,
                "decision": decision,
                "item_shown_at": item_shown_at,
                "decided_at": decided_at,
            }
        )

    def auto_decision(self, account_id: str, role_id: str, *, slow: bool = True) -> None:
        """Add a realistic keep decision for one assignment.

        Picks the campaign of the account's system and a reviewer from the
        pool who is not the account owner. Default timing is 20-300 seconds.
        """
        system = self.acct_meta[account_id]["system"]
        camp = self.campaign_by_system[system]
        owner = self.acct_meta[account_id]["employee_id"]
        pool = [r for r in self.reviewer_pool[system] if r != owner]
        reviewer = pool[int(self.rng.integers(0, len(pool)))]
        start = date.fromisoformat(camp["start_date"])
        shown_dt = datetime.combine(start, time(9, 0)) + timedelta(
            minutes=int(self.rng.integers(0, 240))
        )
        seconds = int(self.rng.integers(20, 301)) if slow else int(self.rng.integers(2, 5))
        decided_dt = shown_dt + timedelta(seconds=seconds)
        self.add_decision(
            camp["campaign_id"],
            account_id,
            role_id,
            reviewer,
            item_shown_at=shown_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            decided_at=decided_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        )


def _build_roles(bank: Bank) -> None:
    for role_id in sorted(ROLES):
        system, name, privileged, _ = ROLES[role_id]
        bank.rows["roles"].append(
            {
                "role_id": role_id,
                "system": system,
                "name": name,
                "privileged": "true" if privileged else "false",
            }
        )
    for parent, child in sorted(HIERARCHY):
        bank.rows["role_hierarchy"].append(
            {
                "parent_role_id": parent,
                "child_role_id": child,
            }
        )
    for ent_id in sorted(ENTITLEMENTS):
        system, name, bf = ENTITLEMENTS[ent_id]
        bank.rows["entitlements"].append(
            {
                "entitlement_id": ent_id,
                "system": system,
                "name": name,
                "business_function": bf,
            }
        )
    for role_id in sorted(ROLES):
        for ent_id in ROLES[role_id][3]:
            bank.rows["role_entitlements"].append(
                {
                    "role_id": role_id,
                    "entitlement_id": ent_id,
                }
            )


def _add_campaigns(bank: Bank) -> None:
    for campaign_id, system, start_off, end_off in CAMPAIGN_PLAN:
        start = bank.as_of - timedelta(days=start_off)
        end = bank.as_of - timedelta(days=end_off)
        row = {
            "campaign_id": campaign_id,
            "system": system,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        bank.rows["recert_campaigns"].append(row)
        bank.campaign_by_system[system] = row
        unit, func = REVIEWER_SOURCE[system]
        bank.reviewer_pool[system] = bank.groups[(unit, func)][:5]


def _add_workforce(bank: Bank) -> None:
    """Employees, personal accounts, standard role assignments, leavers."""
    for unit, _, functions in UNIT_PLAN:
        for func in sorted(functions):
            for _ in range(functions[func]):
                bank.add_employee(unit, func)

    # Clean leavers: exit dates in the past, accounts disabled (near miss 1).
    for unit, _, _ in UNIT_PLAN:
        for func in ("Sachbearbeitung", "Analyse"):
            group = bank.groups.get((unit, func), [])
            for emp_id in group[:2]:
                exit_day = bank.as_of - timedelta(days=int(bank.rng.integers(20, 250)))
                bank.emp_meta[emp_id]["exit_date"] = exit_day
                row = next(r for r in bank.rows["hr_employees"] if r["employee_id"] == emp_id)
                row["exit_date"] = exit_day.isoformat()
                bank.used_employees.add(emp_id)

    for emp_id in sorted(bank.emp_meta):
        meta = bank.emp_meta[emp_id]
        entry = date.fromisoformat(
            next(r for r in bank.rows["hr_employees"] if r["employee_id"] == emp_id)["entry_date"]
        )
        account_id = bank.add_account(
            meta["system"],
            employee_id=emp_id,
            created_at=_ts(entry + timedelta(days=3), 8, 0),
            last_login="" if bank.has_left(emp_id) else bank.personal_login(),
        )
        if bank.has_left(emp_id):
            bank.rows["accounts"][-1]["status"] = "disabled"
        role_id = ROLE_MAP[(meta["unit"], meta["job_function"])]
        bank.assign(account_id, role_id, valid_from=entry.isoformat())


def _add_shared_accounts(bank: Bank) -> None:
    """Technical and generic accounts (near misses 4 and 5)."""
    for system in SYSTEMS:
        for _ in range(TECHNICAL_PLAN[system]):
            never = bank.rng.random() < 0.5
            bank.add_account(
                system,
                account_type="technical",
                interactive=False,
                created_at=_ts(date(2024, 6, 1), 8, 0),
                last_login="" if never else _ts(bank.as_of - timedelta(days=5), 3, 0),
            )
            bank.assign(bank.rows["accounts"][-1]["account_id"], _view_role(system))
        bank.add_account(
            system,
            account_type="generic",
            interactive=False,
            created_at=_ts(date(2023, 3, 1), 8, 0),
            last_login="",
        )
        bank.assign(bank.rows["accounts"][-1]["account_id"], _view_role(system))


def _view_role(system: str) -> str:
    return {
        "PAY": "R-PAY-VIEW",
        "CORE": "R-CRD-VIEW",
        "GL": "R-GL-VIEW",
        "TRADE": "R-TRD-VIEW",
        "AD": "R-AD-RO",
    }[system]


def _add_decisions(bank: Bank) -> None:
    """One keep decision per assignment on an active account of a non-leaver."""
    for account_id in sorted(bank.acct_meta):
        meta = bank.acct_meta[account_id]
        emp_id = meta["employee_id"]
        if not emp_id or bank.has_left(emp_id):
            continue
        for role_id in bank.roles_of(account_id):
            bank.auto_decision(account_id, role_id)


def _add_near_misses(bank: Bank) -> None:
    """Near misses that must exist but must not be flagged (2, 3, 7, 9)."""
    # 2: leaver inside the grace period (exit_date == as_of, active account).
    grace_emp = next(
        e
        for e in bank.groups[("Zahlungsverkehr", "Sachbearbeitung")]
        if e not in bank.used_employees
    )
    bank.used_employees.add(grace_emp)
    exit_row = next(r for r in bank.rows["hr_employees"] if r["employee_id"] == grace_emp)
    exit_row["exit_date"] = bank.as_of.isoformat()
    bank.emp_meta[grace_emp]["exit_date"] = bank.as_of

    # 3: personal account last used exactly 89 days before as_of.
    dorm_emp = next(
        e for e in bank.groups[("Kredit", "Sachbearbeitung")] if e not in bank.used_employees
    )
    bank.used_employees.add(dorm_emp)
    dorm_acct = _account_of(bank, dorm_emp)
    acct_row = next(r for r in bank.rows["accounts"] if r["account_id"] == dorm_acct)
    acct_row["last_login"] = _ts(bank.as_of - timedelta(days=89), 8, 12)

    # 7: two functions that do not conflict (PAYMENT_CREATE + BENEFICIARY_MAINTAIN).
    duo_emp = bank.groups[("Zahlungsverkehr", "Sonderaufgaben")][0]
    bank.used_employees.add(duo_emp)
    duo_acct = _account_of(bank, duo_emp)
    bank.assign(duo_acct, "R-PAY-CLERK")
    bank.assign(duo_acct, "R-PAY-BENE")

    # 9: a role that would be rare, but the peer group has fewer than 5 members.
    rare_emp = bank.groups[("Handel", "Sonderaufgaben")][1]
    bank.used_employees.add(rare_emp)
    bank.assign(_account_of(bank, rare_emp), "R-TRD-TRADER")


def _account_of(bank: Bank, emp_id: str) -> str:
    return next(a for a, m in bank.acct_meta.items() if m["employee_id"] == emp_id)


def _build_clean(as_of: date, rng: np.random.Generator) -> Bank:
    bank = Bank(as_of, rng)
    _build_roles(bank)
    _add_workforce(bank)
    _add_shared_accounts(bank)
    _add_campaigns(bank)
    _add_decisions(bank)
    _add_near_misses(bank)
    return bank


def _write_journal(
    bank: Bank,
    out_dir: Path,
    seed: int,
    inject: bool,
) -> tuple[list[dict[str, str]], Any]:
    """Build, inject, settle and write the module B ledger."""
    from audit_workbench.synth.inject import (
        inject_journal,
        resolve_journal_issues,
    )
    from audit_workbench.synth.ledger import (
        apply_completeness,
        build_ledger,
        check_clean_ledger,
        finalize_ledger,
        plan_completeness,
        write_journal,
    )

    attempt = 0
    problems: list[str] = []
    for attempt in range(8):
        probe_rng = np.random.default_rng([seed, 2, attempt])
        probe = build_ledger(bank, probe_rng)
        finalize_ledger(probe, np.random.default_rng([seed, 3, attempt]))
        problems = check_clean_ledger(probe)
        if not problems:
            break
    else:  # pragma: no cover - the generator is tuned so this does not happen
        raise RuntimeError(f"ledger is not clean after 8 streams: {problems[:5]}")

    rng = np.random.default_rng([seed, 2, attempt])
    ledger = build_ledger(bank, rng)
    issues: list = []
    if inject:
        issues = inject_journal(bank, ledger, rng)
    finalize_ledger(ledger, np.random.default_rng([seed, 3, attempt]))
    if inject:
        plan_completeness(ledger, rng)
        issues += list(apply_completeness(ledger))
        journal_issues = resolve_journal_issues(issues)
    else:
        journal_issues = []
    write_journal(ledger, out_dir)
    return journal_issues, ledger


def _write(
    bank: Bank,
    out_dir: Path,
    seed: int,
    issues: list[dict[str, str]],
    journal_issues: list[dict[str, str]] | None = None,
    change_issues: list[dict[str, str]] | None = None,
) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, columns in SCHEMA.items():
        rows = sorted(bank.rows[name], key=lambda r: tuple(r[c] for c in columns))
        with (out_dir / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    payload: dict[str, Any] = {
        "as_of": bank.as_of.isoformat(),
        "seed": seed,
        "issues": sorted(issues, key=lambda i: (i["rule_id"], i["entity"])),
    }
    if journal_issues is not None:
        from audit_workbench.synth.ledger import JOURNAL_AS_OF

        payload["journal_as_of"] = JOURNAL_AS_OF.isoformat()
        payload["issues"] = sorted(
            issues + journal_issues, key=lambda i: (i["rule_id"], i["entity"])
        )
    if change_issues is not None:
        from audit_workbench.synth.change import CHANGE_AS_OF

        payload["change_as_of"] = CHANGE_AS_OF.isoformat()
        payload["issues"] = sorted(
            payload["issues"] + change_issues, key=lambda i: (i["rule_id"], i["entity"])
        )
    (out_dir / "ground_truth.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def generate_bank(
    out_dir: Path,
    seed: int = 7,
    *,
    inject: bool = True,
    as_of: date = AS_OF,
) -> Path:
    """Write the synthetic bank into *out_dir* and return the directory."""
    rng = np.random.default_rng(seed)
    bank = _build_clean(as_of, rng)
    issues: list[dict[str, str]] = []
    if inject:
        from audit_workbench.synth.inject import inject_issues

        issues = inject_issues(bank)
    journal_issues, _ledger = _write_journal(bank, Path(out_dir), seed, inject)
    # Module C comes last, with its own random stream, so no A or B value moves.
    from audit_workbench.synth.change import generate_change

    change_issues = generate_change(bank, Path(out_dir), seed, inject, issues + journal_issues)
    _write(bank, Path(out_dir), seed, issues, journal_issues, change_issues)
    return Path(out_dir)
