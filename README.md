# RentSafe Lagos

**Know before you sign.** Nigeria's first rental transparency and intelligence
platform — a Glassdoor-meets-Google-Maps for the Lagos rental market. Tenant
reviews, rent history, flood risk, commute intelligence and agent reputation,
built as a mobile-first PWA for real Lagos conditions.

RentSafe does **not** list properties, process payments, or connect landlords
to tenants. It is a pure information layer for the 15M+ tenants of Lagos.

**This repository is the API.** The React PWA lives in
[rentsafe-frontend](https://github.com/ZILLABB/rentsafe-frontend).

## Run it locally (zero setup)

```bash
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

SQLite is created and seeded on first run, so there is nothing to configure.
Docs at http://localhost:8000/docs.

For the production-shaped stack (Postgres + PostGIS, Redis):

```bash
docker compose up --build
```

## Stack

- **Frontend**: React 18 + TypeScript + Tailwind + Framer Motion + TanStack Query, Vite PWA
- **Backend**: FastAPI + SQLAlchemy (async) — SQLite for dev, PostgreSQL 16 + PostGIS for prod
- **Design**: "safety intelligence" system — deep teal ink, band-coloured scores, Plus Jakarta Sans / Inter / JetBrains Mono (see `design/rentsafe-lagos-design.dc.html`)

## Tests

```bash
cd backend && python -m pytest      # 136 tests: identity, scoring, moderation, security + API routes
cd backend && ruff check .          # lint
cd frontend && npm run lint         # eslint
cd frontend && npm run build        # typecheck + production build
```

The API tests cover the boundaries that matter most: OTP throttling, admin RBAC,
the rule that un-approved review content is never served publicly (or leaked via
the activity feed), and the rule that endpoints return null rather than an
invented figure where there's no data. CI runs all of the above plus
`alembic check`, which fails if a model changes without a matching migration.

## Where the data comes from

Reference geography is imported from open datasets. Everything that makes
RentSafe worth using is first-party and always will be.

```bash
cd backend
python -m scripts.import_reference_data --what all --dry-run   # preview
python -m scripts.import_reference_data --what all             # apply
python -m scripts.import_reference_data --what all --offline   # cache only
```

| Data | Source | Licence |
|---|---|---|
| 20 LGAs + 87 neighbourhoods | OpenStreetMap (Overpass) | ODbL 1.0 — attribution required |
| Address search | Nominatim (OSM) | ODbL 1.0 |
| Bus, BRT, ferry, rail stops | OpenStreetMap (Overpass) | ODbL 1.0 |
| Ground elevation | Open-Elevation (SRTM) | public domain |
| Flood banding | derived from elevation | NIHSA coastal-lowland thresholds |

Responses are cached under `data/cache/`, so imports are reproducible
and CI never needs network. Every imported field records its origin in
`data_sources` and is surfaced on the property page — partly because a
transparency product should show its working, and partly because ODbL requires
the attribution.

**What is deliberately not imported, and why:**

- **Rents.** Listing sites publish *asking* prices set by agents. The gap
  between that and what tenants actually pay is the thing this product exists
  to expose — importing it would launder the distortion. Their terms also
  forbid scraping. Rent comes from tenant reports only.
- **Reviews.** First-party by definition. Sourcing opinions about named
  landlords from elsewhere is both a defamation problem and a lie about
  provenance.
- **Commute times.** No open dataset of real door-to-door Lagos times exists.
  A routing API returns its model's estimate — which is precisely what tenant
  reports are meant to correct — so it can only ever be a comparison column,
  never the primary number.

A worked example of why the import matters: the seed had Ilasan Estate at 1.9m
elevation. SRTM measures it at **0m** — sea level — which is exactly why it
floods. Surulere was hand-written at 18m and is actually 4m.

## What isn't built

Stated plainly so nothing on screen implies otherwise:

- **Routing-API comparison.** The integration exists (Google Routes or Mapbox
  Directions, whichever key is configured), but with no key set
  `google_estimate_min` stays null and the commute tab says the comparison
  isn't switched on. It never invents a number.
- **Push delivery at scale.** Web Push works and is wired to area watches, but
  only when `VAPID_*` keys are configured; without them the toggle explains
  that rather than failing silently. Delivery is fired inline on approval — a
  queue is the right answer once there are more watchers than a request can
  fan out to.
- **Map tiles for real traffic.** Explore is a real MapLibre map over
  OpenStreetMap tiles. `tile.openstreetmap.org` is donation-funded and its
  usage policy bars heavy commercial use, so production needs a paid provider
  or a self-hosted tile server. Only the style object changes.
- **Legal review.** The terms and privacy pages are written to be understood
  and are accurate about what the system does, but no Nigerian lawyer has read
  them.

## Operating it

- **`/health/ready`** checks the database and cache and returns 503 when either
  is unreachable — the liveness `/health` only says the process is up.
- **`/metrics`** serves Prometheus text. Counters and latency are per process,
  so scrape each worker as its own target.
- Every response carries **`X-Request-ID`**, echoed from the proxy when present.
  Production logs are JSON and carry that ID on every line. Query strings and
  review text are deliberately never logged — a search term here is usually
  somebody's home address.
- **`TRUSTED_PROXY_HOPS`** must match the number of proxies in front of the app.
  Too high lets callers forge their own IP and defeat every per-IP limit.
- **`MEDIA_BUCKET`** is required outside development. Without it photos sit on
  the container filesystem and a redeploy destroys them with no error anywhere,
  so the app refuses to boot instead.

## Deploying

Local dev needs no configuration. Production does — see `.env.example`.
The app refuses to start with `ENVIRONMENT=production` if `DEBUG` is still true
or either secret is a placeholder, so a misconfigured deploy fails loudly at
boot rather than quietly leaking OTP codes.

Two settings deserve care:

- **`JWT_SECRET`** signs tokens. Rotating it logs everyone out, which is fine.
- **`PHONE_HASH_PEPPER`** salts phone hashes, which are the only route back to a
  user row (plaintext numbers are never stored). It is effectively **immutable**
  for the life of the database — changing it orphans every account with no
  recovery path.

Schema changes go through Alembic (`alembic upgrade head`), not `create_all`;
the auto-create-and-seed path only runs when `DEBUG` is true.
