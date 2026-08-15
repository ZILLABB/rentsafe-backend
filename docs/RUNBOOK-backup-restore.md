# Runbook — backup and restore

**What is irreplaceable:** the reviews. Properties re-import from OpenStreetMap
in minutes and agents from Overture; a tenant's account of living somewhere
exists in exactly one place. Everything below exists for that table.

**The failure this guards against** is not a backup job that crashes — that is
loud, and someone fixes it. It is the quiet one: a job exiting zero every night
for six months, writing files nobody has opened, which turn out to be empty on
the day they are needed.

---

## Daily — automated

```bash
python -m scripts.backup --out /var/backups/rentsafe --keep 14
```

Exits non-zero if the dump is unreadable, truncated, or not a database, and
reports it. A backup job that claims success on a broken dump is worse than no
backup, because it buys false confidence.

### Alerting — two channels, two different questions

```bash
BACKUP_HEARTBEAT_URL=https://hc-ping.com/<uuid>   # pinged only on success
ALERT_WEBHOOK_URL=https://hooks.slack.com/…       # posted to on failure
```

| Channel | When | Catches |
|---|---|---|
| Heartbeat | success only | **the job never ran** — cron removed, container unscheduled, host down |
| Webhook | failure | *why* it failed, when it did run |

The heartbeat matters more than it looks. A failure webhook cannot fire if cron
is gone, the disk filled before the script started, or the container stopped
being scheduled — and every one of those looks exactly like a quiet success
from the inside. An external monitor expecting a daily ping is the only thing
that notices absence.

Set the monitor's period to your schedule plus a grace window (daily backup →
period 1 day, grace 1 hour). Healthchecks.io, Better Stack and Cronitor all do
this on a free tier; any URL that accepts a GET works.

**Test it before trusting it.** Comment out the cron line for a day and check
you actually get paged. An alert nobody has ever seen fire is the same class of
hope as a backup nobody has restored.

What it does:

| Engine | Method | Why |
|---|---|---|
| Postgres | `pg_dump --format=custom` | Runs in one transaction, so the dump is consistent while the app writes |
| SQLite | `VACUUM INTO` | Takes a read lock. `cp` on a live file can capture a torn write |

Verification is not a byte count. Gzipped SQLite dumps are read through to the
end so the **CRC32 trailer** is checked — a dump cut short by a full disk still
begins with a perfectly valid `SQLite format 3` header, so a header check alone
certifies exactly the backups most likely to be broken. Postgres archives are
walked with `pg_restore --list`.

### Store them somewhere else

A backup on the same disk as the database is not a backup. Copy to object
storage, ideally a different provider from the one holding your media:

```bash
aws s3 cp /var/backups/rentsafe/ s3://rentsafe-backups/ --recursive
```

Enable **versioning** and **object lock** on that bucket. Versioning survives a
bad overwrite; object lock survives a compromised key that tries to delete
everything.

---

## Monthly — the drill, done by a person

Restoring somewhere harmless, and looking at the row counts:

```bash
python -m scripts.restore /var/backups/rentsafe/rentsafe-LATEST.sqlite.gz \
  --into /tmp/drill.db
```

```
backup: OK   … — 75,270 bytes gzipped, 466,944 uncompressed, CRC valid
restored to /tmp/drill.db
  reviews      76
  properties   188
  users        6

Drill passed. Nothing live was touched.
```

It **fails** if any of those tables is empty. A restore into an empty database
is not a successful restore, and that is the exact shape the six-months-of-
worthless-backups failure takes.

For Postgres, restore into a scratch database rather than `--into`:

```bash
createdb rentsafe_drill
pg_restore --clean --if-exists --no-owner --dbname=postgresql://…/rentsafe_drill FILE
psql rentsafe_drill -c "SELECT count(*) FROM reviews;"
dropdb rentsafe_drill
```

**Write down the date you last completed a drill.** An untested backup is a
hope. If the date is more than a month old, you do not currently know whether
you can recover.

---

## Incident — restoring for real

Slow down. The classic way to lose data is not a failed backup; it is restoring
the wrong file over a good database at 2am under pressure.

1. **Stop the app.** A running app writing into a half-restored database makes
   the situation worse and muddies what you can recover.

   ```bash
   docker compose stop api    # or scale the service to zero
   ```

2. **Verify the backup before touching anything.**

   ```bash
   python -m scripts.backup --verify-only /var/backups/rentsafe/FILE
   ```

3. **Rehearse it** into a scratch path and read the row counts. Ten seconds
   here has saved entire databases.

   ```bash
   python -m scripts.restore FILE --into /tmp/check.db
   ```

4. **Restore.** `--force` is required, and takes a safety copy of whatever it
   replaces first — the current database is preserved as
   `rentsafe.replaced-<timestamp>.db`.

   ```bash
   python -m scripts.restore FILE --force
   ```

5. **Apply migrations.** The backup may predate a schema change.

   ```bash
   alembic upgrade head
   ```

6. **Start the app and check readiness**, which verifies the database and cache
   rather than just that the process is alive.

   ```bash
   curl -fsS https://api.example.com/health/ready
   ```

7. **Re-import reference data** if the backup was old. Properties, agents,
   elevation and the rent benchmark are all reproducible:

   ```bash
   python -m scripts.import_reference_data --what all
   ```

---

## Media

Photos are **not** in these dumps, deliberately. In production they live in
object storage, whose own durability and versioning are the right mechanism;
copying gigabytes of images into a nightly dump makes it too slow to actually
run, and a backup that gets skipped is not a backup.

What to configure on the media bucket instead:

- **Versioning on.** A deleted or overwritten object stays recoverable.
- **Lifecycle rule** moving non-current versions to cold storage after 30 days.
- **A separate credential** for the app with `PutObject`/`GetObject` only —
  no `DeleteBucket`, no versioning changes.

The app tolerates a missing image (a photo that 404s renders as absent, not as
an error), so media loss degrades the product rather than breaking it. Review
loss does not degrade anything — it ends it.

---

## What is still not covered

Stated plainly so nobody assumes otherwise:

- **No point-in-time recovery.** With nightly dumps you can lose up to 24 hours
  of reviews. If that becomes unacceptable, enable WAL archiving on Postgres —
  this runbook does not set that up.
- **Backups are not encrypted at rest by this script.** They contain phone
  hashes and unpublished review text. Encrypt the bucket, or pipe through `age`
  or `gpg` before upload.
- **The alert channels are wired but not pointed anywhere.** Both settings are
  empty by default, and the code degrades to logging only. Until
  `BACKUP_HEARTBEAT_URL` has a real monitor behind it, a job that stops running
  is still silent.
