"""Phase 4 spec section 1: does an account hold a function on a given day?"""

from __future__ import annotations

from datetime import date

import pytest

from tests.access.builders import MiniBank


def _bank() -> MiniBank:
    bank = MiniBank()
    bank.employee("E1")
    bank.account("GL-1", employee_id="E1", system="GL", created_at="2024-11-09T08:00:00")
    bank.role("R-MANAGER", system="GL").role("R-LEAD", system="GL").role("R-CLERK", system="GL")
    bank.role("R-VIEW", system="GL")
    bank.contains("R-MANAGER", "R-LEAD").contains("R-LEAD", "R-CLERK")
    bank.entitlement("E-POST", "GL_POST", system="GL").entitlement("E-VIEW", "GL_VIEW", system="GL")
    bank.grants("R-CLERK", "E-POST").grants("R-VIEW", "E-VIEW")
    return bank


@pytest.mark.parametrize(
    ("valid_from", "valid_to", "day", "expected"),
    [
        ("2025-03-01", "", date(2025, 2, 28), False),  # day before the assignment
        ("2025-03-01", "", date(2025, 3, 1), True),  # first day counts
        ("2025-03-01", "2025-06-30", date(2025, 6, 30), True),  # last day counts
        ("2025-03-01", "2025-06-30", date(2025, 7, 1), False),  # day after
    ],
)
def test_validity_limits(tmp_path, valid_from, valid_to, day, expected) -> None:
    data = (
        _bank().assign("GL-1", "R-CLERK", valid_from=valid_from, valid_to=valid_to).load(tmp_path)
    )
    assert data.holds_function("GL-1", "GL_POST", day) is expected


def test_function_through_three_levels(tmp_path) -> None:
    data = _bank().assign("GL-1", "R-MANAGER", valid_from="2024-12-01").load(tmp_path)
    assert data.holds_function("GL-1", "GL_POST", date(2025, 5, 27))
    assert data.granting_roles("GL-1", "GL_POST", date(2025, 5, 27)) == ["R-MANAGER"]


def test_other_function_is_not_enough(tmp_path) -> None:
    data = _bank().assign("GL-1", "R-VIEW").load(tmp_path)
    assert not data.holds_function("GL-1", "GL_POST", date(2025, 5, 27))
    assert data.holds_function("GL-1", "GL_VIEW", date(2025, 5, 27))


def test_account_must_exist_on_the_day(tmp_path) -> None:
    data = _bank().assign("GL-1", "R-CLERK", valid_from="2020-01-01").load(tmp_path)
    assert not data.holds_function("GL-1", "GL_POST", date(2024, 11, 8))
    assert data.holds_function("GL-1", "GL_POST", date(2024, 11, 9))


def test_unknown_account_holds_nothing(tmp_path) -> None:
    data = _bank().assign("GL-1", "R-CLERK").load(tmp_path)
    assert not data.holds_function("GL-404", "GL_POST", date(2025, 5, 27))
    assert data.granting_roles("GL-404", "GL_POST", date(2025, 5, 27)) == []


def test_ignores_audit_date(tmp_path) -> None:
    """An assignment that ended before as_of still counts on its own days."""
    data = (
        _bank()
        .assign("GL-1", "R-CLERK", valid_from="2025-01-01", valid_to="2025-12-31")
        .load(tmp_path)
    )
    assert data.as_of == date(2026, 9, 30)
    assert data.holds_function("GL-1", "GL_POST", date(2025, 8, 1))
    assert data.active_assignments.empty


def test_granting_roles_sorted_and_deduplicated(tmp_path) -> None:
    data = (
        _bank()
        .assign("GL-1", "R-LEAD")
        .assign("GL-1", "R-CLERK")
        .assign("GL-1", "R-CLERK", valid_from="2021-01-01")
        .load(tmp_path)
    )
    assert data.granting_roles("GL-1", "GL_POST", date(2025, 1, 2)) == ["R-CLERK", "R-LEAD"]
