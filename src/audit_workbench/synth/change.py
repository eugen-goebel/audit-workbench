"""Synthetic change management data of module C (spec section 4).

``generate_change(bank, out_dir, seed, inject, known_issues)`` is called by
``generate_bank`` *after* all module A and B data exist. It reads the people
and accounts of the finished bank, builds the four change files
(``changes.csv``, ``change_approvals.csv``, ``commits.csv``,
``deployments.csv``) for the audit period 2025-10-01 to 2026-09-30 from its
own random stream (``default_rng([seed, 4])``) and returns the ground-truth
issues of C-01 to C-06. No A or B random draw or counter moves.

Structure
---------

* The cast is taken from the existing HR data (spec section 4): developers
  (``Entwicklung``), operators (``IT-Betrieb``/``Administration``), release
  managers (``IT-Betrieb``/``Sachbearbeitung``), the change advisory board
  (``IT-Betrieb``/``Teamleitung``), requesters (team leads of the business
  units) and testers (``Analyse`` of the business unit). Only employees
  without exit date who joined before the period take part, and nobody who
  is the subject of an A or B ground-truth entry.
* The clean model is built first: every change has a consistent life cycle
  (created, commits, test deployments, test sign-off, approval, production
  deployment) and the near misses of spec section 4 are planted into it.
* ``inject=True`` then mutates picked changes so that each rule fires on
  exactly the entities of the spec table. Entities are references to model
  objects until the ids are assigned, so the ground truth cannot drift from
  the written files.

Near misses and where they are created
--------------------------------------

=== ============================================================ ==========================
 #  Near miss                                                    Created in
=== ============================================================ ==========================
 1  standard change deployed without approval                    ``Builder.standard``
 2  rejected first, approved later, before the deployment        ``Builder.normal`` (4 %)
 3  approval and first deployment at the same timestamp          ``plant_near_misses``
 4  emergency post-approved exactly 7 days after deployment      ``plant_near_misses``
 5  emergency deployed 3 days before ``as_of``, approval open    ``Builder.emergency``
 6  developer deploys own change to test                         ``Builder.test_deployments``
 7  developer deploys an unrelated change (wrote others)         ``plant_near_misses``
 8  pipeline release by a team colleague of the author           ``plant_near_misses``
 9  low-risk normal change without test sign-off                 ``Builder.normal``
10  failed deployment, then successful redeployment              ``Builder.normal``
=== ============================================================ ==========================
"""

from __future__ import annotations

import csv
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from audit_workbench.change.data import SCHEMA as _CHANGE_SCHEMA
from audit_workbench.synth.bank import ROLE_MAP, Bank, _ts

CHANGE_AS_OF = date(2026, 9, 30)
"""Audit date of module C (the same as module A)."""
PERIOD_START = date(2025, 10, 1)
PERIOD_END_TS = datetime(2026, 9, 30, 23, 59, 59)

CHANGE_FILES = ("changes", "change_approvals", "commits", "deployments")

N_STANDARD = 540
N_NORMAL = 1170
N_EMERGENCY = 90
N_UNLINKED_COMMITS = 60

EXPECTED_COUNTS = {"C-01": 6, "C-02": 5, "C-03": 4, "C-04": 4, "C-05": 4, "C-06": 3}

#: changes created later than this cannot finish their life cycle before as_of
LAST_FULL = CHANGE_AS_OF - timedelta(days=35)
#: emergency changes need room for their post-approval window
LAST_EMERGENCY = CHANGE_AS_OF - timedelta(days=12)

SYSTEM_WEIGHTS = [("CORE", 0.24), ("PAY", 0.24), ("GL", 0.20), ("TRADE", 0.20), ("AD", 0.12)]
REQUESTER_SOURCE = {
    "PAY": ("Zahlungsverkehr", "Teamleitung"),
    "CORE": ("Kredit", "Teamleitung"),
    "GL": ("Rechnungswesen", "Teamleitung"),
    "TRADE": ("Handel", "Teamleitung"),
    "AD": ("IT-Betrieb", "Teamleitung"),
}
TESTER_SOURCE = {
    "PAY": ("Zahlungsverkehr", "Analyse"),
    "CORE": ("Kredit", "Analyse"),
    "GL": ("Rechnungswesen", "Analyse"),
    "TRADE": ("Handel", "Analyse"),
    "AD": ("IT-Betrieb", "Analyse"),
}
TITLES_NORMAL = [
    "Release",
    "Schnittstellenanpassung",
    "Datenbankmigration",
    "Performance-Optimierung",
    "Berichtsanpassung",
    "Regulatorische Anpassung",
    "Batch-Verarbeitung",
    "Berechtigungsmodul",
]
TITLES_STANDARD = [
    "Zertifikatserneuerung",
    "Log-Rotation anpassen",
    "Stammdaten-Import",
    "Monatliche Patch-Runde",
    "Konfigurationsparameter setzen",
    "Neustart Batch-Server",
]
TITLES_EMERGENCY = [
    "Hotfix Zahlungsabbruch",
    "Hotfix Abstimmungsfehler",
    "Notfallpatch Sicherheitsluecke",
    "Hotfix Datenkorruption",
    "Notfall-Rollback Konfiguration",
]
P_PIPELINE = 0.70
P_REJECT_FIRST = 0.04
P_CANCELLED = 0.03
NORMAL_ATTEMPTS = [0.85, 0.11, 0.03, 0.01]
STANDARD_ATTEMPTS = [0.90, 0.10]
EMERGENCY_ATTEMPTS = [0.85, 0.15]
# About 15 % of production deployments fail or are rolled back and are then
# redeployed, a realistic change failure rate.

TS_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _fmt(ts: datetime) -> str:
    return ts.strftime(TS_FORMAT)


# ---------------------------------------------------------------------------
# random helpers
# ---------------------------------------------------------------------------


class Gen:
    """Thin wrapper around one numpy generator; every draw goes through it."""

    def __init__(self, rng: np.random.Generator) -> None:
        self.rng = rng

    def ri(self, lo: int, hi: int) -> int:
        """Integer in ``[lo, hi]``."""
        return int(self.rng.integers(lo, hi + 1))

    def chance(self, p: float) -> bool:
        return bool(self.rng.random() < p)

    def pick(self, seq: list[Any]) -> Any:
        return seq[int(self.rng.integers(0, len(seq)))]

    def sample(self, seq: list[Any], n: int) -> list[Any]:
        if len(seq) < n:
            raise RuntimeError(f"change generator: need {n} candidates, have {len(seq)}")
        order = self.rng.permutation(len(seq))[:n]
        return [seq[int(i)] for i in order]

    def weighted(self, items: list[Any], weights: list[float]) -> Any:
        total = sum(weights)
        x = float(self.rng.random()) * total
        acc = 0.0
        for item, weight in zip(items, weights, strict=True):
            acc += weight
            if x < acc:
                return item
        return items[-1]

    def sha(self) -> str:
        return bytes(self.rng.bytes(20)).hex()


def _workdays(lo: date, hi: date) -> list[date]:
    days = []
    day = lo
    while day <= hi:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


WORKDAYS = _workdays(PERIOD_START, CHANGE_AS_OF)


def add_workdays(day: date, n: int) -> date:
    while n > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def rand_workday(g: Gen, lo: date, hi: date) -> date:
    i = bisect_left(WORKDAYS, lo)
    j = bisect_right(WORKDAYS, hi)
    return WORKDAYS[g.ri(i, j - 1)]


def work_ts(g: Gen, day: date, lo: int = 8, hi: int = 17) -> datetime:
    return datetime.combine(day, time(g.ri(lo, hi), g.ri(0, 59), g.ri(0, 59)))


# ---------------------------------------------------------------------------
# people
# ---------------------------------------------------------------------------


@dataclass
class People:
    developers: list[str]
    operators: list[str]
    release_managers: list[str]
    approvers: list[str]
    requesters: dict[str, list[str]]
    testers: dict[str, list[str]]
    account_of: dict[str, str]  # employee -> personal AD account (developers, operators)
    function_of: dict[str, str]  # developer -> job function
    pipeline_account: str

    @classmethod
    def from_bank(cls, bank: Bank, excluded: set[str]) -> People:
        hr = bank.rows["hr_employees"]
        personal_ad: dict[str, list[dict[str, str]]] = {}
        for acc in bank.rows["accounts"]:
            if acc["system"] == "AD" and acc["account_type"] == "personal" and acc["employee_id"]:
                personal_ad.setdefault(acc["employee_id"], []).append(acc)

        def eligible(row: dict[str, str]) -> bool:
            return (
                row["exit_date"] == ""
                and row["entry_date"] < PERIOD_START.isoformat()
                and row["employee_id"] not in excluded
            )

        def group(unit: str, func: str | None, *, needs_account: bool = False) -> list[str]:
            out = []
            for row in sorted(hr, key=lambda r: r["employee_id"]):
                if row["org_unit"] != unit or not eligible(row):
                    continue
                if func is not None and row["job_function"] != func:
                    continue
                if needs_account:
                    accounts = personal_ad.get(row["employee_id"], [])
                    if len(accounts) != 1 or accounts[0]["status"] != "active":
                        continue
                out.append(row["employee_id"])
            return out

        developers = group("Entwicklung", None, needs_account=True)
        operators = group("IT-Betrieb", "Administration", needs_account=True)
        account_of = {e: personal_ad[e][0]["account_id"] for e in developers + operators}
        dev_set = set(developers)
        function_of = {
            r["employee_id"]: r["job_function"] for r in hr if r["employee_id"] in dev_set
        }

        # prefer a technical account that is in use (it has a last login)
        pipeline = sorted(
            (not a["last_login"], a["account_id"])
            for a in bank.rows["accounts"]
            if a["system"] == "AD"
            and a["account_type"] == "technical"
            and a["status"] == "active"
            and a["created_at"] < PERIOD_START.isoformat()
            and a["account_id"] not in excluded
        )
        if not pipeline:
            raise RuntimeError("no technical AD account for the release pipeline")
        people = cls(
            developers=developers,
            operators=operators,
            release_managers=group("IT-Betrieb", "Sachbearbeitung"),
            approvers=group("IT-Betrieb", "Teamleitung"),
            requesters={s: group(*REQUESTER_SOURCE[s]) for s in REQUESTER_SOURCE},
            testers={s: group(*TESTER_SOURCE[s]) for s in TESTER_SOURCE},
            account_of=account_of,
            function_of=function_of,
            pipeline_account=pipeline[0][1],
        )
        for name in ("developers", "operators", "release_managers", "approvers"):
            if len(getattr(people, name)) < 5:
                raise RuntimeError(f"change generator: too few {name}")
        return people


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class Appr:
    stage: str
    approver: str
    decision: str
    ts: datetime


@dataclass(eq=False)
class Commit:
    sha: str
    system: str
    author: str
    ts: datetime
    change: Chg | None


@dataclass(eq=False)
class Dep:
    seq: int
    env: str
    system: str
    ts: datetime
    account: str
    method: str
    released_by: str
    change: Chg | None
    change_ref: str
    result: str
    dep_id: str = ""


@dataclass(eq=False)
class Chg:
    seq: int
    system: str
    ctype: str
    risk: str
    title: str
    requester: str
    created: datetime
    status: str = "closed"
    signoff: datetime | None = None
    tester: str = ""
    approvals: list[Appr] = field(default_factory=list)
    commits: list[Commit] = field(default_factory=list)
    prod: list[Dep] = field(default_factory=list)
    tests: list[Dep] = field(default_factory=list)
    cid: str = ""

    def authors(self) -> list[str]:
        return sorted({c.author for c in self.commits})

    @property
    def first(self) -> Dep:
        return min(self.prod, key=lambda d: (d.ts, d.seq))

    def last_approved_pre(self) -> Appr | None:
        pre = sorted(
            (a for a in self.approvals if a.stage == "pre"),
            key=lambda a: a.ts,
        )
        return pre[-1] if pre and pre[-1].decision == "approved" else None


@dataclass
class Model:
    changes: list[Chg] = field(default_factory=list)
    unlinked_commits: list[Commit] = field(default_factory=list)
    loose_deps: list[Dep] = field(default_factory=list)  # prod deployments outside a change
    builder: Builder | None = None

    def all_deps(self) -> list[Dep]:
        out = list(self.loose_deps)
        for ch in self.changes:
            out += ch.prod + ch.tests
        return out

    def all_commits(self) -> list[Commit]:
        out = list(self.unlinked_commits)
        for ch in self.changes:
            out += ch.commits
        return out


class Builder:
    """Builds the clean change model."""

    def __init__(self, g: Gen, people: People) -> None:
        self.g = g
        self.p = people
        self.seq = 0
        self.dep_seq = 0
        self.shas: set[str] = set()

    # -- small pieces ----------------------------------------------------

    def new_sha(self) -> str:
        while True:
            sha = self.g.sha()
            if sha not in self.shas:
                self.shas.add(sha)
                return sha

    def change(self, ctype: str, system: str, created: datetime, risk: str, title: str) -> Chg:
        self.seq += 1
        return Chg(
            seq=self.seq,
            system=system,
            ctype=ctype,
            risk=risk,
            title=title,
            requester=self.g.pick(self.p.requesters[system]),
            created=created,
        )

    def system(self) -> str:
        return self.g.weighted([s for s, _ in SYSTEM_WEIGHTS], [w for _, w in SYSTEM_WEIGHTS])

    def add_commit(self, ch: Chg, author: str, ts: datetime) -> Commit:
        commit = Commit(self.new_sha(), ch.system, author, ts, ch)
        ch.commits.append(commit)
        return commit

    def add_dep(
        self,
        env: str,
        system: str,
        ts: datetime,
        account: str,
        method: str,
        released_by: str,
        change: Chg | None,
        result: str,
        change_ref: str = "",
    ) -> Dep:
        self.dep_seq += 1
        return Dep(
            self.dep_seq, env, system, ts, account, method, released_by, change, change_ref, result
        )

    def deployer(self, method: str) -> tuple[str, str]:
        """``(account_id, released_by_employee_id)`` for a production deployment."""
        if method == "pipeline":
            return self.p.pipeline_account, self.g.pick(self.p.release_managers)
        return self.p.account_of[self.g.pick(self.p.operators)], ""

    def authors_for(self, n_authors_weights: list[float]) -> list[str]:
        n = self.g.weighted([1, 2, 3], n_authors_weights)
        return self.g.sample(self.p.developers, n)

    def commits_over_days(
        self, ch: Chg, authors: list[str], n: int, span: int, *, first_offset: int = 1
    ) -> None:
        """*n* commits on working days ``first_offset .. span`` after creation."""
        g = self.g
        n = max(n, len(authors))
        offsets = sorted(g.ri(first_offset, span) for _ in range(n))
        order = authors + [g.pick(authors) for _ in range(n - len(authors))]
        for author, off in zip(order, offsets, strict=True):
            ts = work_ts(g, add_workdays(ch.created.date(), off), 8, 18)
            if ts <= PERIOD_END_TS:
                self.add_commit(ch, author, ts)

    def test_deployments(self, ch: Chg, n: int) -> None:
        """Developers deploy their own commits to test (near miss 6)."""
        g = self.g
        if not ch.commits:
            return
        for commit in g.sample(ch.commits, min(n, len(ch.commits))):
            ts = commit.ts + timedelta(minutes=g.ri(10, 120))
            if ts > PERIOD_END_TS:
                continue
            result = "success" if g.chance(0.9) else "failed"
            ch.tests.append(
                self.add_dep(
                    "test",
                    ch.system,
                    ts,
                    self.p.account_of[commit.author],
                    "manual",
                    "",
                    ch,
                    result,
                )
            )

    def attempts(
        self,
        ch: Chg,
        first_ts: datetime,
        n: int,
        method: str,
        *,
        minutes: bool = False,
    ) -> None:
        """*n* production deployments: failed/rolled back ones, then a success."""
        g = self.g
        ts = first_ts
        account, released_by = self.deployer(method)
        for i in range(n):
            if i and g.chance(0.25):
                account, released_by = self.deployer(method)
            result = "success" if i == n - 1 else g.weighted(["failed", "rolled_back"], [0.7, 0.3])
            ch.prod.append(
                self.add_dep("prod", ch.system, ts, account, method, released_by, ch, result)
            )
            if minutes:
                ts = ts + timedelta(minutes=g.ri(30, 180))
            elif g.chance(0.5):
                ts = ts + timedelta(hours=g.ri(2, 5))
            else:
                ts = work_ts(g, add_workdays(ts.date(), 1))

    def approver(self, ch: Chg) -> str:
        return self.g.pick([a for a in self.p.approvers if a != ch.requester])

    # -- change types ------------------------------------------------------

    def normal(self, day: date) -> Chg:
        g = self.g
        system = self.system()
        risk = g.weighted(["low", "medium", "high"], [0.50, 0.35, 0.15])
        ch = self.change(
            "normal",
            system,
            work_ts(g, day, 8, 16),
            risk,
            f"{g.pick(TITLES_NORMAL)} {system}-{g.ri(100, 999)}",
        )
        in_progress = day > LAST_FULL
        cancelled = (not in_progress) and g.chance(P_CANCELLED)
        ch.status = "in_progress" if in_progress else "cancelled" if cancelled else "closed"

        authors = self.authors_for([0.55, 0.35, 0.10])
        if cancelled:
            self.commits_over_days(ch, authors, g.ri(1, 3), 3)
            if g.chance(0.5):
                day_r = add_workdays(ch.commits[-1].ts.date(), g.ri(1, 2))
                ch.approvals.append(
                    Appr("pre", self.approver(ch), "rejected", work_ts(g, day_r, 9, 16))
                )
            return ch
        self.commits_over_days(ch, authors, g.ri(2, 8), g.ri(2, 6))
        self.test_deployments(ch, g.weighted([1, 2, 3, 4, 5], [0.15, 0.25, 0.3, 0.2, 0.1]))
        if in_progress:
            return ch

        last = max([c.ts for c in ch.commits] + [t.ts for t in ch.tests])
        # near miss 9: a low-risk change often has no test sign-off at all
        if risk != "low" or g.chance(0.55):
            ch.signoff = work_ts(g, add_workdays(last.date(), g.ri(1, 2)), 9, 15)
            ch.tester = g.pick(self.p.testers[system])
        base = ch.signoff.date() if ch.signoff else last.date()
        if g.chance(P_REJECT_FIRST):  # near miss 2
            rej_day = add_workdays(base, 1)
            ch.approvals.append(
                Appr("pre", self.approver(ch), "rejected", work_ts(g, rej_day, 9, 16))
            )
            appr_day = add_workdays(rej_day, g.ri(1, 2))
        else:
            appr_day = add_workdays(base, g.ri(1, 2))
        ch.approvals.append(Appr("pre", self.approver(ch), "approved", work_ts(g, appr_day, 9, 16)))
        first_ts = work_ts(g, add_workdays(appr_day, g.ri(1, 2)))
        n = g.weighted([1, 2, 3, 4], NORMAL_ATTEMPTS)  # near miss 10
        self.attempts(ch, first_ts, n, "pipeline" if g.chance(P_PIPELINE) else "manual")
        return ch

    def standard(self, day: date) -> Chg:
        g = self.g
        system = self.system()
        risk = g.weighted(["low", "medium"], [0.9, 0.1])
        ch = self.change(
            "standard",
            system,
            work_ts(g, day, 8, 16),
            risk,
            f"{g.pick(TITLES_STANDARD)} {system}",
        )
        if day > LAST_FULL:
            ch.status = "in_progress"
            return ch
        base = ch.created.date()
        if g.chance(0.4):
            self.commits_over_days(ch, self.g.sample(self.p.developers, 1), 1, 1)
            base = ch.commits[0].ts.date() if ch.commits else base
        first_ts = work_ts(g, add_workdays(base, g.ri(1, 2)))
        n = g.weighted([1, 2], STANDARD_ATTEMPTS)
        # near miss 1: standard changes are deployed without any approval
        self.attempts(ch, first_ts, n, "pipeline" if g.chance(P_PIPELINE) else "manual")
        return ch

    def emergency(
        self,
        created: datetime,
        *,
        post_days: int | None = None,
        post: bool = True,
    ) -> Chg:
        """An emergency change. ``post_days`` fixes the post-approval date offset."""
        g = self.g
        system = self.system()
        ch = self.change(
            "emergency",
            system,
            created,
            g.weighted(["high", "medium"], [0.6, 0.4]),
            f"{g.pick(TITLES_EMERGENCY)} {system}",
        )
        authors = self.g.sample(self.p.developers, g.weighted([1, 2], [0.7, 0.3]))
        ts = created
        for i in range(max(len(authors), g.ri(1, 3))):
            ts = ts + timedelta(minutes=g.ri(20, 180))
            author = authors[i] if i < len(authors) else g.pick(authors)
            self.add_commit(ch, author, ts)
        if g.chance(0.7):
            last = ch.commits[-1]
            ts = last.ts + timedelta(minutes=g.ri(10, 40))
            ch.tests.append(
                self.add_dep(
                    "test", system, ts, self.p.account_of[last.author], "manual", "", ch, "success"
                )
            )
        first_ts = ts + timedelta(minutes=g.ri(20, 120))
        n = g.weighted([1, 2], EMERGENCY_ATTEMPTS)
        self.attempts(ch, first_ts, n, "pipeline" if g.chance(0.5) else "manual", minutes=True)
        if post:
            first = ch.first
            days: int = (
                post_days
                if post_days is not None
                else g.weighted([0, 1, 2, 3, 4, 5, 6], [0.15, 0.2, 0.2, 0.15, 0.1, 0.1, 0.1])
            )
            if days == 0:
                approved = first.ts + timedelta(hours=g.ri(1, 4))
            else:
                approved = work_ts(g, first.ts.date() + timedelta(days=days), 9, 16)
            ch.approvals.append(Appr("post", self.approver(ch), "approved", approved))
        return ch


def build_clean(g: Gen, people: People) -> Model:
    b = Builder(g, people)
    model = Model()
    first_day = PERIOD_START
    for _ in range(N_NORMAL):
        model.changes.append(b.normal(rand_workday(g, first_day, CHANGE_AS_OF)))
    for _ in range(N_STANDARD):
        model.changes.append(b.standard(rand_workday(g, first_day, CHANGE_AS_OF)))

    # near miss 5: emergencies deployed 3 and 7 days before as_of, post-approval still open
    for days_before in (3, 7):
        created = datetime.combine(CHANGE_AS_OF - timedelta(days=days_before), time(10, 30, 0))
        model.changes.append(b.emergency(created + timedelta(minutes=g.ri(0, 90)), post=False))
    span = (LAST_EMERGENCY - first_day).days
    for _ in range(N_EMERGENCY - 2):
        day = first_day + timedelta(days=g.ri(0, span))
        created = datetime.combine(day, time(g.ri(0, 23), g.ri(0, 59), g.ri(0, 59)))
        model.changes.append(b.emergency(created))

    # unlinked commits: work that has not been tied to a change yet
    for _ in range(N_UNLINKED_COMMITS):
        ts = work_ts(g, rand_workday(g, first_day, CHANGE_AS_OF), 8, 18)
        model.unlinked_commits.append(
            Commit(b.new_sha(), b.system(), g.pick(people.developers), ts, None)
        )
    # ids follow the creation order
    model.changes.sort(key=lambda c: (c.created, c.seq))
    for i, ch in enumerate(model.changes, start=1):
        ch.cid = f"CHG-{i:05d}"
    b.seq = max(b.seq, len(model.changes))
    model.builder = b
    return model


# ---------------------------------------------------------------------------
# near misses (clean data)
# ---------------------------------------------------------------------------


def _plain(model: Model, reserved: set[int], ctype: str = "normal") -> list[Chg]:
    return [
        c
        for c in model.changes
        if c.ctype == ctype and c.status == "closed" and c.prod and c.seq not in reserved
    ]


def plant_near_misses(g: Gen, people: People, model: Model, reserved: set[int]) -> None:
    # 3: approval and first production deployment at the same timestamp (C-04)
    pool = [c for c in _plain(model, reserved) if c.last_approved_pre() is not None]
    for ch in g.sample(pool, 2):
        approval = ch.last_approved_pre()
        assert approval is not None
        ch.first.ts = approval.ts
        reserved.add(ch.seq)

    # 4: emergency post-approved exactly 7 days after the first deployment (C-03)
    pool = [
        c
        for c in _plain(model, reserved, "emergency")
        if any(a.stage == "post" for a in c.approvals)
        and c.first.ts.date() <= CHANGE_AS_OF - timedelta(days=20)
    ]
    for ch in g.sample(pool, 2):
        post = next(a for a in ch.approvals if a.stage == "post")
        post.ts = work_ts(g, ch.first.ts.date() + timedelta(days=7), 9, 16)
        reserved.add(ch.seq)

    # 7: a developer who wrote commits for other changes deploys an unrelated one (C-02)
    singles = [c for c in _plain(model, reserved) if len(c.prod) == 1]
    singles_std = [c for c in _plain(model, reserved, "standard") if len(c.prod) == 1]
    for ch in g.sample(singles, 2) + g.sample(singles_std, 1):
        authors = set(ch.authors())
        dev = g.pick([d for d in people.developers if d not in authors])
        for dep in ch.prod:
            dep.method = "manual"
            dep.account = people.account_of[dev]
            dep.released_by = ""
        reserved.add(ch.seq)

    # 8: pipeline release by a team colleague of the author (C-02)
    pool = [
        c
        for c in _plain(model, reserved)
        if c.commits and all(d.method == "pipeline" for d in c.prod)
    ]
    for ch in g.sample(pool, 3):
        authors = ch.authors()
        author = g.pick(authors)
        peers = [
            d
            for d in people.developers
            if d not in authors and people.function_of[d] == people.function_of[author]
        ]
        colleague = g.pick(peers or [d for d in people.developers if d not in authors])
        for dep in ch.prod:
            dep.released_by = colleague
        reserved.add(ch.seq)


# ---------------------------------------------------------------------------
# injection
# ---------------------------------------------------------------------------


@dataclass
class Issue:
    rule_id: str
    target: Chg | Dep
    note: str  # may contain {entity}


def _single(model: Model, reserved: set[int]) -> list[Chg]:
    return [c for c in _plain(model, reserved) if len(c.prod) == 1 and c.last_approved_pre()]


def _add_second_account(g: Gen, bank: Bank, employee: str, dep: Dep) -> str:
    """Give *employee* a second personal AD account (appended after all A and B data).

    Standard role of the peer group, recent login and a keep decision of the
    AD campaign, so every module A rule stays quiet.
    """
    meta = bank.emp_meta[employee]
    role = ROLE_MAP[(meta["unit"], meta["job_function"])]
    created_day = dep.ts.date() - timedelta(days=g.ri(10, 40))
    login_day = bank.as_of - timedelta(days=g.ri(1, 30))
    account_id = bank.add_account(
        "AD",
        employee_id=employee,
        created_at=_ts(created_day, 8, 0),
        last_login=_ts(login_day, g.ri(7, 18), g.ri(0, 59)),
    )
    bank.assign(account_id, role, valid_from=created_day.isoformat())
    camp = bank.campaign_by_system["AD"]
    pool = [r for r in bank.reviewer_pool["AD"] if r != employee]
    reviewer = pool[g.ri(0, len(pool) - 1)]
    shown = datetime.combine(date.fromisoformat(camp["start_date"]), time(9, 0)) + timedelta(
        minutes=g.ri(0, 239)
    )
    decided = shown + timedelta(seconds=g.ri(20, 300))
    bank.add_decision(
        camp["campaign_id"],
        account_id,
        role,
        reviewer,
        item_shown_at=_fmt(shown),
        decided_at=_fmt(decided),
    )
    return account_id


def inject_changes(
    g: Gen, bank: Bank, people: People, model: Model, reserved: set[int]
) -> list[Issue]:
    b = model.builder
    assert b is not None
    issues: list[Issue] = []
    self_deployer: dict[int, str] = {}

    def take(pool: list[Chg], n: int) -> list[Chg]:
        picked = g.sample([c for c in pool if c.seq not in reserved], n)
        reserved.update(c.seq for c in picked)
        return picked

    def pipeline(dep: Dep, released_by: str) -> None:
        dep.method = "pipeline"
        dep.account = people.pipeline_account
        dep.released_by = released_by

    def manual(dep: Dep, account: str) -> None:
        dep.method = "manual"
        dep.account = account
        dep.released_by = ""

    # -- C-02: developer deployed their own change -------------------------
    early = datetime(2025, 11, 15)
    late = datetime(2026, 8, 1)
    singles = _single(model, reserved)
    own_a, own_b = take(singles, 2)
    second = take([c for c in singles if early <= c.first.ts <= late], 1)[0]
    pipe_a, pipe_b = take(singles, 2)
    for ch in (own_a, own_b):
        author = g.pick(ch.authors())
        self_deployer[ch.seq] = author
        manual(ch.first, people.account_of[author])
        issues.append(
            Issue(
                "C-02",
                ch.first,
                f"{{entity}}: {author} deployed their own change {ch.cid} manually with "
                f"their primary account",
            )
        )
    author = g.pick(second.authors())
    account = _add_second_account(g, bank, author, second.first)
    manual(second.first, account)
    issues.append(
        Issue(
            "C-02",
            second.first,
            f"{{entity}}: {author} deployed their own change {second.cid} manually with "
            f"the second AD account {account}",
        )
    )
    for ch in (pipe_a, pipe_b):
        author = g.pick(ch.authors())
        pipeline(ch.first, author)
        issues.append(
            Issue(
                "C-02",
                ch.first,
                f"{{entity}}: {author} released their own change {ch.cid} through the pipeline",
            )
        )

    # -- C-01: deployment without an approved change -----------------------
    for method, system in (("pipeline", "CORE"), ("manual", "TRADE")):
        ts = work_ts(g, rand_workday(g, date(2025, 10, 6), date(2026, 9, 25)))
        account, released_by = b.deployer(method)
        dep = b.add_dep("prod", system, ts, account, method, released_by, None, "success")
        model.loose_deps.append(dep)
        issues.append(
            Issue("C-01", dep, f"{{entity}}: production deployment on {system} without change id")
        )
    ts = work_ts(g, rand_workday(g, date(2025, 10, 6), date(2026, 9, 25)))
    account, released_by = b.deployer("pipeline")
    ghost = f"CHG-{len(model.changes) + 4711:05d}"
    dep = b.add_dep("prod", "PAY", ts, account, "pipeline", released_by, None, "success", ghost)
    model.loose_deps.append(dep)
    issues.append(Issue("C-01", dep, f"{{entity}}: change {ghost} does not exist"))

    # a ticket of one system reused for the deployment on another system
    reused = take(_single(model, reserved), 1)[0]
    other = g.pick([s for s in ("CORE", "PAY", "GL", "TRADE", "AD") if s != reused.system])
    reused.first.system = other
    issues.append(
        Issue(
            "C-01",
            reused.first,
            f"{{entity}}: change {reused.cid} belongs to {reused.system}, deployed on {other}",
        )
    )

    unapproved, cancelled = take(_single(model, reserved), 2)
    kept = unapproved.last_approved_pre()
    assert kept is not None
    kept.decision = "rejected"
    unapproved.approvals = [kept]
    issues.append(
        Issue(
            "C-01",
            unapproved.first,
            f"{{entity}}: change {unapproved.cid} was rejected and never approved",
        )
    )
    cancelled.status = "cancelled"
    issues.append(
        Issue("C-01", cancelled.first, f"{{entity}}: change {cancelled.cid} is cancelled")
    )

    # -- C-04: approval after the deployment (one also lacks test evidence) -
    def late_enough(c: Chg) -> bool:
        return c.first.ts <= PERIOD_END_TS - timedelta(days=8)

    approved = [c for c in _plain(model, reserved) if c.last_approved_pre() and late_enough(c)]
    risky = [c for c in approved if c.risk != "low" and c.signoff is not None]
    overlap = take(risky, 1)[0]
    c04 = [overlap] + take(approved, 3)
    shifts = [
        timedelta(hours=5, minutes=17),
        timedelta(hours=20, minutes=4),
        timedelta(days=2, hours=3),
        timedelta(days=5, hours=8),
    ]
    for ch, shift in zip(c04, shifts, strict=True):
        approval = ch.last_approved_pre()
        assert approval is not None
        approval.ts = ch.first.ts + shift
        hours = round(shift.total_seconds() / 3600, 2)
        issues.append(
            Issue("C-04", ch, f"{ch.cid}: approved {hours} hours after the first deployment")
        )
    # C-06 after_deploy on the same change (deliberate overlap)
    overlap.signoff = overlap.first.ts + timedelta(hours=2)
    issues.append(
        Issue("C-06", overlap, f"{overlap.cid}: test sign-off 2 hours after the first deployment")
    )

    # -- C-06: missing and self-tested sign-off -----------------------------
    risky = [c for c in _plain(model, reserved) if c.risk != "low" and c.signoff is not None]
    missing, selftested = take(risky, 2)
    missing.signoff = None
    missing.tester = ""
    issues.append(Issue("C-06", missing, f"{missing.cid}: no test sign-off"))
    tester = g.pick(selftested.authors())
    selftested.tester = tester
    issues.append(Issue("C-06", selftested, f"{selftested.cid}: tested by its author {tester}"))

    # -- C-05: approver not independent --------------------------------------
    plain = [c for c in _plain(model, reserved) if c.last_approved_pre()]
    req_normal, auth_normal = take(plain, 2)
    for ch in (req_normal,):
        for appr in ch.approvals:
            if appr.decision == "approved":
                appr.approver = ch.requester
        issues.append(
            Issue("C-05", ch, f"{ch.cid}: approved by its requester {ch.requester} (pre)")
        )
    author = g.pick(auth_normal.authors())
    for appr in auth_normal.approvals:
        if appr.decision == "approved":
            appr.approver = author
    issues.append(Issue("C-05", auth_normal, f"{auth_normal.cid}: approved by its author {author}"))

    emerg = [
        c
        for c in _plain(model, reserved, "emergency")
        if any(a.stage == "post" for a in c.approvals)
        and c.first.ts.date() <= CHANGE_AS_OF - timedelta(days=20)
    ]
    req_emerg = take(emerg, 1)[0]
    for appr in req_emerg.approvals:
        appr.approver = req_emerg.requester
    issues.append(
        Issue(
            "C-05",
            req_emerg,
            f"{req_emerg.cid}: emergency change post-approved by its requester "
            f"{req_emerg.requester}",
        )
    )
    # approver = author on a self-deployed change (deliberate overlap with C-02)
    overlap_c02 = own_b
    author = self_deployer[overlap_c02.seq]
    for appr in overlap_c02.approvals:
        if appr.decision == "approved":
            appr.approver = author
    issues.append(
        Issue(
            "C-05",
            overlap_c02,
            f"{overlap_c02.cid}: approved by its author {author}, who also deployed it",
        )
    )

    # -- C-03: emergency changes without (timely) post-approval ---------------
    emerg = [
        c
        for c in _plain(model, reserved, "emergency")
        if any(a.stage == "post" for a in c.approvals)
        and c.first.ts.date() <= CHANGE_AS_OF - timedelta(days=20)
    ]
    miss_a, miss_b, late_8, late_15 = take(emerg, 4)
    for ch in (miss_a, miss_b):
        ch.approvals = []
        issues.append(
            Issue(
                "C-03",
                ch,
                f"{ch.cid}: emergency change without post-approval since "
                f"{ch.first.ts.date().isoformat()}",
            )
        )
    for ch, days in ((late_8, 8), (late_15, 15)):
        post = next(a for a in ch.approvals if a.stage == "post")
        post.ts = work_ts(g, ch.first.ts.date() + timedelta(days=days), 9, 16)
        issues.append(
            Issue("C-03", ch, f"{ch.cid}: emergency change post-approved {days} days late")
        )

    return issues


def _commit_days(ch: Chg) -> list[date]:
    """Working days on which an extra commit fits into the life cycle of *ch*.

    Strictly after the creation day and strictly before the day of the first
    sign-off, approval or production deployment, so the commit precedes them.
    """
    stamps = [ch.first.ts] + [a.ts for a in ch.approvals]
    if ch.signoff:
        stamps.append(ch.signoff)
    lo = ch.created.date() + timedelta(days=1)
    hi = min(stamps).date() - timedelta(days=1)
    return _workdays(lo, hi) if lo <= hi else []


def inject_crosslink_activity(
    g: Gen,
    bank: Bank,
    model: Model,
    reserved: set[int],
    known_issues: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """X-03 and its near misses (phase 4 spec, section 6, item 3).

    * The two developer leavers of A-01 (account still active, Entwicklung) each
      author one commit on a change after their exit date. The change is deployed
      by an operator or release manager, never by the leaver. Their ``last_login``
      moves after the commit, so A-01 reports ``logged_in_after_exit``; the note of
      that A-01 ground-truth row is rewritten in place.
    * Near miss: a leaver (disabled account) commits on the exit date itself.
    * Near miss: a leaver (disabled account) commits only before the exit date.

    Commits go to unreserved, closed normal changes, so no C rule is touched.
    """
    hr = {r["employee_id"]: r for r in bank.rows["hr_employees"]}
    accounts = {r["account_id"]: r for r in bank.rows["accounts"]}
    leavers = []
    for issue in known_issues:
        if issue["rule_id"] != "A-01":
            continue
        account = accounts[issue["entity"]]
        employee = account["employee_id"]
        if hr[employee]["org_unit"] == "Entwicklung":
            leavers.append((employee, issue))
    if len(leavers) != 2:
        raise RuntimeError(f"expected 2 developer leavers in A-01, found {len(leavers)}")

    pool = [
        (c, _commit_days(c))
        for c in model.changes
        if c.ctype == "normal" and c.status == "closed" and c.prod and c.seq not in reserved
    ]
    pool = [(c, days) for c, days in pool if days]
    chosen: set[int] = set()
    b = model.builder
    assert b is not None

    def add_commit(employee: str, accept: Any) -> tuple[Commit, Chg]:
        options = [
            (c, [d for d in days if accept(d)])
            for c, days in pool
            if c.seq not in chosen and any(accept(d) for d in days)
        ]
        if not options:
            raise RuntimeError(f"no change fits a commit of {employee}")
        ch, days = g.pick(options)
        day = g.pick(days)
        chosen.add(ch.seq)
        sha = bytes(g.rng.bytes(20)).hex()
        while sha in b.shas:
            sha = bytes(g.rng.bytes(20)).hex()
        b.shas.add(sha)
        commit = Commit(sha, ch.system, employee, work_ts(g, day, 8, 18), ch)
        ch.commits.append(commit)
        return commit, ch

    out: list[dict[str, str]] = []
    for employee, issue in leavers:
        exit_day = date.fromisoformat(hr[employee]["exit_date"])
        commit, ch = add_commit(employee, lambda d, x=exit_day: d > x)
        row = accounts[issue["entity"]]
        login = commit.ts + timedelta(minutes=g.ri(5, 90))
        row["last_login"] = _fmt(login)
        issue["note"] = (
            f"Account {issue['entity']} of leaver {employee} (exit {exit_day}) "
            f"is still active and was used after the exit"
        )
        days_after = (commit.ts.date() - exit_day).days
        out.append(
            {
                "rule_id": "X-03",
                "entity": employee,
                "note": (
                    f"Leaver {employee} (exit {exit_day}) authored commit {commit.sha[:8]} "
                    f"on {commit.ts.date()}, {days_after} days after the exit, on change "
                    f"{ch.cid}, which someone else deployed"
                ),
            }
        )

    # near misses: disabled leavers of Entwicklung (exit dates inside the period)
    clean = [
        e
        for func in ("Sachbearbeitung", "Analyse")
        for e in bank.groups[("Entwicklung", func)][:2]
        if hr[e]["exit_date"] and accounts[_account_of_employee(bank, e)]["status"] == "disabled"
    ]
    on_exit = before_exit = ""
    for employee in clean:
        exit_day = date.fromisoformat(hr[employee]["exit_date"])
        if not on_exit and exit_day.weekday() < 5:
            try:
                add_commit(employee, lambda d, x=exit_day: d == x)
            except RuntimeError:
                continue
            on_exit = employee
        elif not before_exit:
            try:
                add_commit(employee, lambda d, x=exit_day: d < x)
            except RuntimeError:
                continue
            before_exit = employee
    if not (on_exit and before_exit):
        raise RuntimeError("no leaver found for the X-03 near misses")
    bank.crosslink["x03_near_miss"] = {"on_exit_date": on_exit, "before_exit": before_exit}
    return out


def _account_of_employee(bank: Bank, employee: str) -> str:
    return next(a for a, m in bank.acct_meta.items() if m["employee_id"] == employee)


def _count(issues: list[Issue]) -> dict[str, int]:
    out: dict[str, int] = {}
    for issue in issues:
        out[issue.rule_id] = out.get(issue.rule_id, 0) + 1
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def _check_period(model: Model) -> None:
    start = datetime.combine(PERIOD_START, time(0, 0))
    stamps = [c.created for c in model.changes]
    stamps += [c.ts for c in model.all_commits()]
    stamps += [d.ts for d in model.all_deps()]
    stamps += [a.ts for c in model.changes for a in c.approvals]
    stamps += [c.signoff for c in model.changes if c.signoff]
    bad = [t for t in stamps if not (start <= t <= PERIOD_END_TS)]
    if bad:
        raise RuntimeError(f"change data outside the audit period: {sorted(bad)[:3]}")


def _rows(model: Model) -> dict[str, list[dict[str, str]]]:
    deps = sorted(model.all_deps(), key=lambda d: (d.ts, d.seq))
    for i, dep in enumerate(deps, start=1):
        dep.dep_id = f"DEP-{i:06d}"
    changes = [
        {
            "change_id": c.cid,
            "system": c.system,
            "change_type": c.ctype,
            "risk": c.risk,
            "title": c.title,
            "requester_employee_id": c.requester,
            "created_ts": _fmt(c.created),
            "test_signoff_ts": _fmt(c.signoff) if c.signoff else "",
            "tester_employee_id": c.tester,
            "status": c.status,
        }
        for c in model.changes
    ]
    approvals = [
        {
            "change_id": c.cid,
            "approval_stage": a.stage,
            "approver_employee_id": a.approver,
            "decision": a.decision,
            "decided_ts": _fmt(a.ts),
        }
        for c in model.changes
        for a in c.approvals
    ]
    commits = [
        {
            "commit_sha": k.sha,
            "system": k.system,
            "author_employee_id": k.author,
            "committed_ts": _fmt(k.ts),
            "change_id": k.change.cid if k.change else "",
        }
        for k in model.all_commits()
    ]
    deployments = [
        {
            "deployment_id": d.dep_id,
            "system": d.system,
            "environment": d.env,
            "deployed_ts": _fmt(d.ts),
            "account_id": d.account,
            "method": d.method,
            "released_by_employee_id": d.released_by,
            "change_id": d.change.cid if d.change else d.change_ref,
            "result": d.result,
        }
        for d in deps
    ]
    approvals.sort(
        key=lambda r: (
            r["change_id"],
            r["decided_ts"],
            r["approval_stage"],
            r["approver_employee_id"],
        )
    )
    commits.sort(key=lambda r: (r["committed_ts"], r["commit_sha"]))
    return {
        "changes": changes,
        "change_approvals": approvals,
        "commits": commits,
        "deployments": deployments,
    }


def write_change(rows: dict[str, list[dict[str, str]]], out_dir: Path) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in CHANGE_FILES:
        columns = _CHANGE_SCHEMA[name]
        with (out_dir / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows[name])


def _excluded(bank: Bank, known_issues: list[dict[str, Any]]) -> set[str]:
    """Employees and accounts that the A and B ground truth already talks about."""
    entities = {str(i["entity"]) for i in known_issues}
    owners = {a["employee_id"] for a in bank.rows["accounts"] if a["account_id"] in entities}
    return entities | owners | set(bank.used_employees)


def generate_change(
    bank: Bank,
    out_dir: Path,
    seed: int,
    inject: bool,
    known_issues: list[dict[str, Any]] | None = None,
    crosslink_issues: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Write the four change files; return the ground-truth issues (empty without injection).

    With ``inject=True`` and a list for *crosslink_issues*, the X-03 cases of phase 4
    are added after all C draws (own random stream ``bank.xrng``): extra commits of
    leavers, appended to *crosslink_issues*. The A-01 notes and ``last_login`` of the
    two developer leavers in *known_issues* and ``bank`` change accordingly.

    With ``inject=True`` one extra personal AD account (with role assignment
    and recertification decision) is appended to ``bank.rows``; the caller
    writes the module A files afterwards.
    """
    g = Gen(np.random.default_rng([seed, 4]))
    people = People.from_bank(bank, _excluded(bank, known_issues or []))
    model = build_clean(g, people)
    reserved: set[int] = set()
    plant_near_misses(g, people, model, reserved)
    issues: list[Issue] = []
    if inject:
        issues = inject_changes(g, bank, people, model, reserved)
        counts = _count(issues)
        # C-04 and C-06 share a change and so do C-02 and C-05: count per rule
        if counts != EXPECTED_COUNTS:
            raise RuntimeError(f"injected counts {counts} != spec {EXPECTED_COUNTS}")
    if inject and crosslink_issues is not None:
        if bank.xrng is None:
            raise RuntimeError("bank.xrng is not set")
        crosslink_issues += inject_crosslink_activity(
            Gen(bank.xrng), bank, model, reserved, known_issues or []
        )
    _check_period(model)
    rows = _rows(model)
    write_change(rows, Path(out_dir))
    out = []
    for issue in issues:
        entity = issue.target.cid if isinstance(issue.target, Chg) else issue.target.dep_id
        out.append(
            {
                "rule_id": issue.rule_id,
                "entity": entity,
                "note": issue.note.replace("{entity}", entity),
            }
        )
    return out
