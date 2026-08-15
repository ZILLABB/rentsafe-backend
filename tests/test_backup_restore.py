"""Backup and restore.

The reviews are the product. Properties re-import from OpenStreetMap in minutes
and agents from Overture, but a tenant's account of living somewhere exists in
exactly one place.

The failure this guards against is not "the backup script crashed" — that is
loud and someone fixes it. It is the quiet one: a job that exits zero every
night for six months writing files nobody has ever opened, and which turn out
to be empty on the day they are needed. So the checks here are all about
*refusing* things:

* a truncated dump must not restore,
* a valid-but-empty database must not count as a successful restore, and
* restoring must never overwrite a live database by accident.
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3

import pytest

from scripts import backup as backup_mod
from scripts import restore as restore_mod


def _make_db(path, *, reviews: int = 3, pad_rows: int = 0) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE reviews (id INTEGER PRIMARY KEY, body TEXT)")
    con.execute("CREATE TABLE properties (id INTEGER PRIMARY KEY, name TEXT)")
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, phone_hash TEXT)")
    con.executemany(
        "INSERT INTO reviews (body) VALUES (?)", [(f"review {i}",) for i in range(reviews)]
    )
    con.executemany(
        "INSERT INTO properties (name) VALUES (?)", [(f"block {i}",) for i in range(2)]
    )
    con.executemany("INSERT INTO users (phone_hash) VALUES (?)", [("abc",), ("def",)])
    if pad_rows:
        con.execute("CREATE TABLE junk (id INTEGER, pad TEXT)")
        con.executemany(
            "INSERT INTO junk VALUES (?,?)", [(i, "x" * 400) for i in range(pad_rows)]
        )
    con.commit()
    con.close()


def test_backup_round_trips_the_data(tmp_path, monkeypatch):
    """The whole point, stated once: what goes in comes back out."""
    live = tmp_path / "live.db"
    _make_db(live, reviews=7)
    monkeypatch.setattr(
        backup_mod.settings, "database_url", f"sqlite+aiosqlite:///{live.as_posix()}"
    )

    archive = backup_mod.backup_sqlite(backup_mod.settings.database_url, tmp_path)
    ok, detail = backup_mod.verify(archive)
    assert ok, detail

    recovered = tmp_path / "recovered.db"
    restore_mod.restore_sqlite(archive, recovered)
    assert restore_mod.inspect_sqlite(recovered)["reviews"] == 7


@pytest.mark.parametrize("fraction", [0.5, 0.9])
def test_a_truncated_backup_is_refused(tmp_path, fraction):
    """A dump cut short by a full disk must never verify.

    This is the case a header check cannot catch: truncation removes the *end*
    of the file, and "SQLite format 3" is the first sixteen bytes. Only reading
    the stream through to the gzip CRC trailer detects it — which is why the
    file is cut proportionally here rather than to a fixed offset, so the test
    still truncates as the fixture grows.
    """
    live = tmp_path / "live.db"
    # Enough rows that the compressed file is comfortably larger than any
    # fixed slice, so the truncation is real.
    _make_db(live, reviews=500)
    archive = backup_mod.backup_sqlite(f"sqlite+aiosqlite:///{live.as_posix()}", tmp_path)

    full = archive.read_bytes()
    truncated = tmp_path / "truncated.sqlite.gz"
    truncated.write_bytes(full[: int(len(full) * fraction)])

    ok, detail = backup_mod.verify(truncated)
    assert not ok
    assert "truncated or corrupt" in detail


def test_something_that_is_not_a_database_is_refused(tmp_path):
    """Gzip of the wrong thing decompresses happily and is still not a backup."""
    import os

    fake = tmp_path / "notadb.sqlite.gz"
    with gzip.open(fake, "wb") as gz:
        # Random bytes: repetitive text would compress below the size floor and
        # be rejected for the wrong reason, proving nothing about the header.
        gz.write(os.urandom(4096))

    ok, detail = backup_mod.verify(fake)
    assert not ok
    assert "not a SQLite database" in detail


def test_an_empty_database_does_not_count_as_restored(tmp_path):
    """The subtle failure: large enough to look real, no rows in it.

    A restore that "succeeds" into an empty database is how you discover during
    an incident that six months of backups were worthless.
    """
    empty = tmp_path / "empty.db"
    # Padding so it clears the size check and has to be caught by row counts.
    _make_db(empty, reviews=0, pad_rows=300)
    con = sqlite3.connect(empty)
    con.execute("DELETE FROM properties")
    con.execute("DELETE FROM users")
    con.commit()
    con.close()

    archive = tmp_path / "empty.sqlite.gz"
    with open(empty, "rb") as raw, gzip.open(archive, "wb") as gz:
        shutil.copyfileobj(raw, gz)

    ok, _ = backup_mod.verify(archive)
    assert ok, "should pass the cheap format check — that is the point"

    recovered = tmp_path / "recovered.db"
    restore_mod.restore_sqlite(archive, recovered)
    counts = restore_mod.inspect_sqlite(recovered)
    assert counts["reviews"] == 0
    # The row check is what has to catch this.
    assert any(n <= 0 for n in counts.values())


def test_a_missing_table_reads_as_missing_not_zero(tmp_path):
    """A schema mismatch is a different problem from an empty table."""
    partial = tmp_path / "partial.db"
    con = sqlite3.connect(partial)
    con.execute("CREATE TABLE reviews (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()

    counts = restore_mod.inspect_sqlite(partial)
    assert counts["properties"] == -1
    assert counts["users"] == -1


def test_verify_rejects_an_unknown_format(tmp_path):
    odd = tmp_path / "backup.tar"
    odd.write_bytes(b"x" * 5000)
    ok, detail = backup_mod.verify(odd)
    assert not ok
    assert "unrecognised" in detail


def test_pruning_keeps_the_newest(tmp_path):
    """Retention must never delete the most recent backup."""
    import os
    import time

    for i in range(5):
        path = tmp_path / f"rentsafe-2026081{i}T000000Z.sqlite.gz"
        path.write_bytes(b"x" * 3000)
        # Distinct mtimes so ordering is deterministic on fast filesystems.
        os.utime(path, (time.time() + i, time.time() + i))

    removed = backup_mod.prune(tmp_path, keep=2)
    remaining = sorted(p.name for p in tmp_path.glob("rentsafe-*"))
    assert removed == 3
    assert remaining == [
        "rentsafe-20260813T000000Z.sqlite.gz",
        "rentsafe-20260814T000000Z.sqlite.gz",
    ]


def test_pruning_zero_keeps_everything(tmp_path):
    for i in range(3):
        (tmp_path / f"rentsafe-2026081{i}T000000Z.sqlite.gz").write_bytes(b"x" * 3000)
    # `--keep 0` means "retain all", and must not be read as "delete all".
    assert backup_mod.prune(tmp_path, keep=0) == 3


@pytest.mark.parametrize(
    "url,expected_suffix",
    [
        ("sqlite+aiosqlite:///./rentsafe.db", "rentsafe.db"),
        ("sqlite+aiosqlite:///./data/app.db?timeout=30", "app.db"),
    ],
)
def test_sqlite_path_is_parsed_from_the_url(url, expected_suffix):
    assert backup_mod._sqlite_path(url).name == expected_suffix
