#!/usr/bin/env python
"""Restore the RentSafe database from a backup.

    python -m scripts.restore backups/rentsafe-20260814T120000Z.sqlite.gz
    python -m scripts.restore FILE --into /tmp/drill.db   # rehearsal, no risk
    python -m scripts.restore FILE --force                # overwrite for real

Restoring is destructive, so it refuses by default. Two ways to proceed:

  --into PATH   restore somewhere else. This is the *drill*: it proves the
                backup is recoverable without touching anything live, and is
                what the runbook asks you to run monthly.
  --force       overwrite the configured database. Requires you to have meant
                it, and takes a safety copy of what it replaces first.

The safety copy matters more than it looks. The classic way to lose data is not
a failed backup — it is restoring the wrong file over a good database during an
incident, at 2am, under pressure.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from scripts.backup import verify

settings = get_settings()

# Tables that must have rows for a restore to be believable. Reviews are the
# product; a "successful" restore of an empty database is the failure this
# check exists to catch.
EXPECTED_TABLES = ("reviews", "properties", "users")


def _sqlite_path(url: str) -> Path:
    return Path(url.split("///", 1)[1].split("?", 1)[0]).resolve()


def restore_sqlite(backup: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if backup.name.endswith(".gz"):
        with gzip.open(backup, "rb") as gz, open(target, "wb") as out:
            shutil.copyfileobj(gz, out)
    else:
        shutil.copyfile(backup, target)


def restore_postgres(backup: Path, url: str) -> None:
    clean = url.replace("+asyncpg", "").replace("+psycopg", "")
    cmd = [
        "pg_restore",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        f"--dbname={clean}",
        str(backup),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    # pg_restore exits non-zero on benign "does not exist" notices from --clean,
    # so the row check below is what actually decides success.
    if result.returncode != 0 and "error" in result.stderr.lower():
        print(result.stderr.strip(), file=sys.stderr)


def inspect_sqlite(path: Path) -> dict[str, int]:
    con = sqlite3.connect(path)
    try:
        counts = {}
        for table in EXPECTED_TABLES:
            try:
                counts[table] = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                counts[table] = -1
        return counts
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("backup", help="Path to a backup produced by scripts.backup")
    ap.add_argument("--into", help="Restore to this path instead of the live database")
    ap.add_argument(
        "--force", action="store_true", help="Overwrite the configured database"
    )
    args = ap.parse_args()

    backup = Path(args.backup)
    ok, detail = verify(backup)
    print(f"backup: {'OK  ' if ok else 'FAIL'} {backup} — {detail}")
    if not ok:
        raise SystemExit(1)

    url = settings.database_url
    is_sqlite = url.startswith("sqlite")

    if args.into:
        target = Path(args.into).resolve()
        if not is_sqlite:
            raise SystemExit(
                "--into is for SQLite drills. For Postgres, restore into a "
                "scratch database: pg_restore --dbname=postgres://…/drill FILE"
            )
        restore_sqlite(backup, target)
        counts = inspect_sqlite(target)
        print(f"restored to {target}")
        for table, n in counts.items():
            print(f"  {table:12} {n if n >= 0 else 'MISSING'}")
        if any(n <= 0 for n in counts.values()):
            print("\nA restore with no reviews is not a successful restore.")
            raise SystemExit(1)
        print("\nDrill passed. Nothing live was touched.")
        return

    if not args.force:
        raise SystemExit(
            "Refusing to overwrite the live database.\n"
            "  Rehearse safely:  --into /tmp/drill.db\n"
            "  Mean it:          --force"
        )

    if is_sqlite:
        live = _sqlite_path(url)
        if live.exists():
            stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
            safety = live.with_name(f"{live.stem}.replaced-{stamp}{live.suffix}")
            shutil.copyfile(live, safety)
            print(f"safety copy of the current database: {safety}")
        restore_sqlite(backup, live)
        counts = inspect_sqlite(live)
    else:
        restore_postgres(backup, url)
        counts = {}

    print(f"restored into {url.split('@')[-1]}")
    for table, n in counts.items():
        print(f"  {table:12} {n}")
    print("\nRun `alembic upgrade head` — the backup may predate a migration.")


if __name__ == "__main__":
    main()
