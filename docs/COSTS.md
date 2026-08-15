# What RentSafe costs to run

Prices checked 15 August 2026. **USD→NGN 1,358.6** on the day; naira figures move
with the rate, so treat the dollar column as the stable one.

Assumptions are listed with each number so you can argue with them rather than
inherit them. Where a figure is uncertain it says so.

---

## The short answer

| Stage | Users | Monthly | What dominates |
|---|---|---|---|
| **Pilot** | 100 | **₦12,000** (~$9) | Fixed infrastructure |
| **Growing** | 1,000 | **₦42,000–46,000** (~$31–34) | Map tiles |
| **Scale** | 10,000 | **₦347,000–379,000** (~$255–279) | Map tiles, then SMS |

Self-hosted (Hetzner VPS + Cloudflare R2), tiles cached. A managed platform
(Render) adds roughly **₦18,000/month** on top for the convenience.

Plus **one-off legal costs** — the largest single line item before launch, and
the one I can't price for you. See below.

---

## Fixed monthly

Two shapes. The difference is whether you want to run the box.

| | Self-hosted | Managed (Render) |
|---|---|---|
| App | Hetzner CX22 $6 | Starter $7 |
| Postgres | on the same box | Basic $7 |
| Redis | on the same box | $5 |
| Object storage (R2, ~20GB media) | $0.30 | $0.30 |
| Heartbeat monitor | free tier | free tier |
| Error tracking (Sentry) | free tier | free tier |
| **Subtotal** | **$6.30 — ₦8,559** | **$19.30 — ₦26,221** |

Domain is ~$12/year, so about ₦1,400/month amortised.

**Cloudflare R2 rather than S3** for one specific reason: R2 charges no egress.
Media is images served to phones, so egress is the dominant cost on S3 and zero
on R2. The `S3MediaStore` already supports it — set `MEDIA_ENDPOINT_URL`.

---

## Variable — and the surprise is not SMS

I expected SMS to dominate. It doesn't. **Map tiles do**, by roughly 5×.

| Users | SMS/month | Tiles/month |
|---|---|---|
| 100 | ₦325–650 | ₦3,057 |
| 1,000 | ₦3,250–6,500 | ₦30,568 |
| 10,000 | ₦32,500–65,000 | ₦305,685 |

### Tiles

Assumes ~$0.50 per 1,000 tiles, which is typical paid raster pricing, and that
each user pulls ~30 tiles on a first visit plus ~15 more across later sessions.

The 30-day tile cache already in the service worker is doing real work here:

| Users | Uncached | Cached | Saved/month |
|---|---|---|---|
| 1,000 | ₦67,930 | ₦30,568 | **₦37,362** |
| 10,000 | ₦679,300 | ₦305,685 | **₦373,615** |

Two ways to cut it further, both worth doing before 10,000 users:

- **Self-host the tiles.** Lagos alone at zoom 0–16 is a small extract — a few
  GB, servable from the same VPS with `tileserver-gl` or a static pyramid on R2.
  This turns a per-request cost into a fixed one and is the single biggest
  lever in this whole document.
- **Don't load the map until asked.** The property list is the useful part; the
  map is already lazy-loaded for bundle size, and making it load on interaction
  rather than on paint would cut tile fetches for anyone who came to check one
  address.

Note the current tile source is `tile.openstreetmap.org`, which is
donation-funded and **bars heavy commercial use**. It costs nothing and is not
an option at scale — the figures above are what you pay when you move off it.

### SMS

Every sign-in sends one OTP. Refresh tokens last 30 days, so an active user
signs in about once a month; the model assumes 1.3 to cover resends and
mistyped codes.

Nigerian bulk providers quote **₦1.80–₦5.00 per message**, transactional/DND
routes at the upper end. Termii is quoted elsewhere at $0.0107 (~₦14.5), which
is far above the local range — most likely a different route or an
international rate. **Confirm your actual per-message price with Termii before
relying on the SMS line**; it's the one number here I am least confident in.

SMS is also the only cost with no free tier anywhere. Without it nobody can
sign in, so nobody can review anything.

### Routing and push — currently zero

Valhalla via FOSSGIS costs nothing and needs no key. A traffic-aware provider
would add roughly $5 per 1,000 requests (Google Routes), cached an hour, so at
1,000 users checking a couple of commutes a month that's ~₦13,600/month. The
free-flow number is honest and labelled as such, so this is genuinely optional.

Web Push is free — VAPID keys are self-generated.

---

## What runs at zero today

- Valhalla routing (FOSSGIS)
- OSM tiles — *non-commercial use only*
- Healthchecks.io heartbeat, Sentry (5k events/month)
- Web Push
- OpenStreetMap and Overture imports
- NBS rent benchmark

---

## One-off, before launch

These are the real numbers and I can't source them for you — they depend on who
you engage.

| | Why it isn't optional |
|---|---|
| **Nigerian lawyer** — terms, privacy, liability | The app hosts accusations about named landlords and agents. Somebody has to decide who is liable when one sues: you, or the tenant who wrote it. The legal pages say themselves that no lawyer has read them. |
| **NDPC registration** | Nigeria requires data controllers to register. You process phone numbers of Lagos residents. |
| **Termii account** | Business verification and a sender ID. |

Expect the legal work to exceed a year of infrastructure. That is the correct
ratio for this product, not a sign something is wrong.

---

## Ongoing, not infrastructure

**A moderator.** Reviews land in a queue and stay invisible until a human
approves them; the SLA on the admin page is 72 hours. At 1,000 users that is
maybe an hour a day. If nobody does it, publishing silently stops and looks
identical to the app being broken.

This is the largest recurring cost in the whole product and it does not appear
on any invoice.

---

## What I would actually do

1. **Pilot on ₦12,000/month.** One VPS, R2, free tiers, OSM tiles while usage is
   genuinely non-commercial. Buy SMS credit in the smallest useful block.
2. **Move tiles before 1,000 users**, self-hosted for Lagos only. It is the
   dominant variable cost and the cheapest to fix.
3. **Spend on the lawyer before anything else.** Infrastructure at this scale is
   noise; a defamation claim is not.

The engineering is cheap to run. The expensive parts are a lawyer and a person
reading reviews every day — neither of which is a server.
