#!/usr/bin/env python
"""Take a consistent backup of the RentSafe database.

    python -m scripts.backup                      # write to ./backups
    python -m scripts.backup --out /var/backups
    python -m scripts.backup --verify-only FILE   # check an existing backup

Why this exists: the reviews are the product. Properties can be re-imported
from OpenStreetMap and agents from Overture in minutes, but a tenant's account
of living somewhere exists in exactly one place. Losing the database loses
that, and there is no second copy to fall back on.

Two engines, two correct tools:

  Postgres   pg_dump in custom format. Runs inside a single transaction, so the
             dump is consistent even while the app is writing.
  SQLite     VACUUM INTO. Takes a read lock and writes a defragmented copy —
             unlike `cp`, which can capture a torn file mid-write.

Media is deliberately *not* in here. In production it lives in object storage,
whose durability and versioning are the right mechanism; copying gigabytes of
photos into a nightly dump would make the dump too slow to actually run. See
the runbook for what to enable on the bucket.
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
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings

settings = get_settings()

# Only catches genuinely broken output — a zero-length file, or an error
# message written where a dump should be. Deliberately low: a young database
# with a handful of reviews compresses to a few hundred bytes, and failing a
# healthy backup for being small would train whoever reads the alerts to
# ignore them. Whether the *contents* are plausible is the restore drill's job,
# not a byte count's.
MIN_PLAUSIBLE_BYTES = 128


def _sqlite_path(url: str) -> Path:
    """Filesystem path out of a SQLAlchemy SQLite URL."""
    tail = url.split("///", 1)[1]
    return Path(tail.split("?", 1)[0]).resolve()


def backup_sqlite(url: str, out_dir: Path) -> Path:
    """Consistent copy via VACUUM INTO.

    `cp` on a live SQLite file can capture a write in progress and produce a
    database that opens fine and is missing the last transaction. VACUUM INTO
    takes a read lock for the duration.
    """
    source = _sqlite_path(url)
    if not source.exists():
        raise SystemExit(f"No database at {source}")

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"rentsafe-{stamp}.sqlite"

    con = sqlite3.connect(source)
    try:
        # Parameter binding is not allowed for the target path here.
        con.execute(f"VACUUM INTO '{target.as_posix()}'")
    finally:
        con.close()

    compressed = target.with_suffix(".sqlite.gz")
    with open(target, "rb") as raw, gzip.open(compressed, "wb") as gz:
        shutil.copyfileobj(raw, gz)
    target.unlink()
    return compressed


def backup_postgres(url: str, out_dir: Path) -> Path:
    """pg_dump in custom format, which is compressed and restorable selectively."""
    parsed = urlparse(url.replace("+asyncpg", "").replace("+psycopg", ""))
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"rentsafe-{stamp}.dump"

    cmd = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        f"--file={target}",
        url.replace("+asyncpg", "").replace("+psycopg", ""),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"pg_dump failed:\n{result.stderr.strip()}")
    if not parsed.hostname:
        raise SystemExit("DATABASE_URL has no host; refusing to guess")
    return target


def verify(path: Path) -> tuple[bool, str]:
    """Check a backup is readable and non-trivial.

    A backup nobody has opened is a hope, not a backup. This is the cheap half
    of that — the runbook covers the full restore drill, which is the half that
    actually proves recoverability.
    """
    if not path.exists():
        return False, "file does not exist"
    size = path.stat().st_size
    if size < MIN_PLAUSIBLE_BYTES:
        return False, f"only {size} bytes — not a dump at all"

    # The header is the real check. Size only rules out an empty file; a
    # truncated dump is caught here, because gzip cannot read past the cut and
    # a partial pg_dump loses its marker.
    if path.name.endswith(".sqlite.gz"):
        # Read the whole stream, not just the header. A dump truncated by a full
        # disk still starts with a perfectly good "SQLite format 3" — the header
        # is the first sixteen bytes — so checking it alone certifies exactly
        # the backups most likely to be broken. Gzip carries a CRC32 and length
        # trailer, and decompressing to the end is what verifies them.
        uncompressed = 0
        header = b""
        try:
            with gzip.open(path, "rb") as gz:
                while chunk := gz.read(1 << 20):
                    if not header:
                        header = chunk[:16]
                    uncompressed += len(chunk)
        except (OSError, EOFError) as exc:
            return False, f"truncated or corrupt: {type(exc).__name__} {exc}"

        if not header.startswith(b"SQLite format 3"):
            return False, "decompresses, but is not a SQLite database"
        return True, f"{size:,} bytes gzipped, {uncompressed:,} uncompressed, CRC valid"

    if path.suffix == ".dump":
        with open(path, "rb") as fh:
            if fh.read(5) != b"PGDMP":
                return False, "missing the PGDMP marker"
        # The marker only proves it started life as a dump. `pg_restore --list`
        # walks the table of contents and is the real integrity check, so use it
        # when the binary is available and say so plainly when it is not.
        try:
            listed = subprocess.run(
                ["pg_restore", "--list", str(path)],
                capture_output=True, text=True, check=False,
            )
        except FileNotFoundError:
            return True, f"{size:,} bytes, PGDMP marker (pg_restore absent — TOC unchecked)"
        if listed.returncode != 0:
            return False, f"pg_restore could not read the archive: {listed.stderr.strip()[:120]}"
        entries = len([ln for ln in listed.stdout.splitlines() if ln and not ln.startswith(";")])
        return True, f"{size:,} bytes, {entries} archive entries"

    return False, f"unrecognised backup format: {path.name}"


def prune(out_dir: Path, keep: int) -> int:
    """Delete all but the newest `keep` backups."""
    backups = sorted(
        [p for p in out_dir.glob("rentsafe-*") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for old in backups[keep:]:
        old.unlink()
        removed += 1
    return removed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="backups", help="Directory to write into")
    ap.add_argument(
        "--keep", type=int, default=14, help="How many backups to retain (0 = all)"
    )
    ap.add_argument("--verify-only", help="Verify an existing backup and exit")
    args = ap.parse_args()

    if args.verify_only:
        ok, detail = verify(Path(args.verify_only))
        print(f"{'OK  ' if ok else 'FAIL'} {args.verify_only}: {detail}")
        raise SystemExit(0 if ok else 1)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    url = settings.database_url
    path = backup_sqlite(url, out_dir) if url.startswith("sqlite") else backup_postgres(url, out_dir)

    ok, detail = verify(path)
    print(f"{'OK  ' if ok else 'FAIL'} {path}: {detail}")
    if not ok:
        # Leave the bad file in place for inspection, but fail loudly: a backup
        # job that exits zero on a broken dump is worse than none, because it
        # buys false confidence.
        raise SystemExit(1)

    if args.keep:
        pruned = prune(out_dir, args.keep)
        if pruned:
            print(f"     pruned {pruned} older backup(s), keeping {args.keep}")


if __name__ == "__main__":
    main()
