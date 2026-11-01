"""Anomaly ranking of manual entries with an Isolation Forest (not a rule).

The score is a ranking aid for sample selection: it sorts manual entries by how
unusual they look on amount, time of day, weekday, distance to the period end,
how common their account pair is and how much their preparer posts. It has no
threshold, so it is not an audit test and produces no findings; the auditor
still has to explain why a top-ranked entry is worth selecting.
"""

from __future__ import annotations

import calendar

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

from audit_workbench.journal.data import JournalData

FEATURES = [
    "amount",
    "hour",
    "weekday",
    "days_to_period_end",
    "pair_frequency",
    "preparer_volume",
]
"""Features of the score, in the order of the model matrix."""

RANDOM_STATE = 0


def entry_features(data: JournalData) -> pd.DataFrame:
    """The score features for every manual entry, one row per doc_no.

    The amount is a float here because it only feeds a model; nothing is
    compared against a limit.
    """
    m = data.manual_entries
    if m.empty:
        return pd.DataFrame(
            columns=["doc_no", "preparer_employee_id", *FEATURES],
        )
    pairs = m["doc_no"].map(
        lambda d: max((data.pair_counts[p] for p in data.entry_pairs[d]), default=0)
    )

    ts = m["entry_ts"]
    weekday = ts.dt.day_of_week
    month_end = m["posting_date"].map(lambda d: calendar.monthrange(d.year, d.month)[1])
    frame = pd.DataFrame(
        {
            "doc_no": m["doc_no"].to_numpy(),
            "preparer_employee_id": m["preparer_employee_id"].to_numpy(),
            "amount": m["amount_cents"].to_numpy(dtype=float) / 100,
            "hour": (ts.dt.hour + ts.dt.minute / 60).to_numpy(),
            "weekday": weekday.to_numpy(),
            "days_to_period_end": (month_end - m["posting_date"].map(lambda d: d.day)).to_numpy(),
            "pair_frequency": pairs.to_numpy(dtype=float),
            "preparer_volume": m["preparer_employee_id"]
            .map(m["preparer_employee_id"].value_counts())
            .fillna(0)
            .to_numpy(dtype=float),
        }
    )
    return frame.sort_values("doc_no", kind="stable").reset_index(drop=True)


def rank_entries(data: JournalData, top: int | None = None) -> pd.DataFrame:
    """Rank manual entries by anomaly score, most unusual first.

    Columns: `rank`, `score` (higher is more unusual), `doc_no`, the preparer
    and the feature values. Ties are broken by doc_no, so the ranking is
    deterministic for the same data.
    """
    features = entry_features(data)
    if features.empty:
        return pd.DataFrame(columns=["rank", "score", "doc_no", "preparer_employee_id", *FEATURES])

    matrix = np.column_stack(
        [
            np.log10(features["amount"].to_numpy() + 1),
            features["hour"].to_numpy(),
            features["weekday"].to_numpy(),
            features["days_to_period_end"].to_numpy(),
            np.log1p(features["pair_frequency"].to_numpy()),
            np.log1p(features["preparer_volume"].to_numpy()),
        ]
    )
    model = IsolationForest(n_estimators=200, random_state=RANDOM_STATE)
    model.fit(matrix)
    ranked = features.assign(score=-model.score_samples(matrix))
    ranked = ranked.sort_values(["score", "doc_no"], ascending=[False, True], kind="stable")
    ranked = ranked.reset_index(drop=True)
    ranked.insert(0, "rank", np.arange(1, len(ranked) + 1))
    if top is not None:
        ranked = ranked.head(max(top, 0))
    columns = ["rank", "score", "doc_no", "preparer_employee_id", *FEATURES]
    return ranked[columns].reset_index(drop=True)
