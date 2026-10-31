"""Tests for loading input files as hashed, validated evidence."""

import hashlib
from pathlib import Path

import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st

from audit_workbench.core.evidence import EvidenceError, load_evidence, sha256_file


def write_csv(path: Path, text: str) -> Path:
    path.write_bytes(text.encode("utf-8"))
    return path


def test_sha256_matches_hashlib(tmp_path):
    f = write_csv(tmp_path / "users.csv", "user_id,name\nU1,Anna\n")
    assert sha256_file(f) == hashlib.sha256(f.read_bytes()).hexdigest()


def test_load_returns_frame_and_metadata(tmp_path):
    f = write_csv(tmp_path / "users.csv", "user_id,name\nU1,Anna\nU2,Ben\n")
    ev = load_evidence(f, name="hr_users", required_columns=["user_id", "name"])
    assert ev.name == "hr_users"
    assert ev.rows == 2
    assert ev.sha256 == sha256_file(f)
    assert list(ev.frame["user_id"]) == ["U1", "U2"]
    assert ev.loaded_at.tzinfo is not None


def test_missing_column_is_rejected(tmp_path):
    f = write_csv(tmp_path / "users.csv", "user_id\nU1\n")
    with pytest.raises(EvidenceError, match="name"):
        load_evidence(f, name="hr_users", required_columns=["user_id", "name"])


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(EvidenceError, match="not found"):
        load_evidence(tmp_path / "nope.csv", name="x", required_columns=[])


def test_unsupported_format_is_rejected(tmp_path):
    f = tmp_path / "data.xlsx"
    f.write_bytes(b"x")
    with pytest.raises(EvidenceError, match="format"):
        load_evidence(f, name="x", required_columns=[])


def test_parquet_is_supported(tmp_path):
    pytest.importorskip("pyarrow")
    f = tmp_path / "users.parquet"
    pd.DataFrame({"user_id": ["U1"]}).to_parquet(f)
    assert load_evidence(f, name="x", required_columns=["user_id"]).rows == 1


def test_values_are_read_as_text_by_default(tmp_path):
    # IDs like "007" must not turn into the number 7, or joins silently break.
    f = write_csv(tmp_path / "users.csv", "user_id\n007\n")
    ev = load_evidence(f, name="x", required_columns=["user_id"])
    assert ev.frame["user_id"].iloc[0] == "007"


@given(st.binary(min_size=1, max_size=2048), st.integers(min_value=0))
def test_hash_changes_when_one_byte_changes(tmp_path_factory, data, pos):
    d = tmp_path_factory.mktemp("h")
    a = d / "a.bin"
    b = d / "b.bin"
    a.write_bytes(data)
    i = pos % len(data)
    changed = bytearray(data)
    changed[i] = (changed[i] + 1) % 256
    b.write_bytes(bytes(changed))
    assert sha256_file(a) == sha256_file(a)
    assert sha256_file(a) != sha256_file(b)
