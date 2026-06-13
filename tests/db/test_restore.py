"""Tests for swarm.db.core restore helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm.db.core import SwarmDB, find_latest_backup, restore_backup


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "swarm.db"


def _seed(db_path: Path, value: str) -> None:
    db = SwarmDB(db_path)
    db.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('marker', ?)", (value,))
    db.commit()
    db.close()


def _read_marker(db_path: Path) -> str | None:
    db = SwarmDB(db_path)
    row = db.fetchone("SELECT value FROM config WHERE key = 'marker'")
    db.close()
    return row[0] if row else None


class TestRestoreBackup:
    def test_round_trip(self, db_path: Path, tmp_path: Path) -> None:
        _seed(db_path, "original")
        db = SwarmDB(db_path)
        backup = db.backup(tmp_path / "snap.db")
        db.close()
        _seed(db_path, "mutated")
        assert _read_marker(db_path) == "mutated"

        restore_backup(backup, db_path=db_path)
        assert _read_marker(db_path) == "original"

    def test_preserves_pre_restore_copy(self, db_path: Path, tmp_path: Path) -> None:
        _seed(db_path, "original")
        db = SwarmDB(db_path)
        backup = db.backup(tmp_path / "snap.db")
        db.close()
        _seed(db_path, "mutated")

        restore_backup(backup, db_path=db_path)
        pre = db_path.with_suffix(".db.pre-restore")
        assert pre.exists()
        assert _read_marker(pre) == "mutated"

    def test_missing_backup_raises(self, db_path: Path, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            restore_backup(tmp_path / "nope.db", db_path=db_path)

    def test_corrupt_backup_rejected(self, db_path: Path, tmp_path: Path) -> None:
        _seed(db_path, "original")
        garbage = tmp_path / "garbage.db"
        garbage.write_bytes(b"not a sqlite file at all")
        with pytest.raises(ValueError):
            restore_backup(garbage, db_path=db_path)
        # Original untouched
        assert _read_marker(db_path) == "original"


class TestFindLatestBackup:
    def test_picks_newest(self, tmp_path: Path) -> None:
        old = tmp_path / "swarm_20260101_000000.db"
        new = tmp_path / "swarm_20260613_120000.db"
        old.write_bytes(b"x")
        new.write_bytes(b"y")
        import os

        os.utime(old, (1, 1))
        assert find_latest_backup(tmp_path) == new

    def test_empty_dir_returns_none(self, tmp_path: Path) -> None:
        assert find_latest_backup(tmp_path) is None

    def test_missing_dir_returns_none(self, tmp_path: Path) -> None:
        assert find_latest_backup(tmp_path / "absent") is None
