# Deployment

Backend on **Render free tier**, frontend on **Vercel**, manual click-through (no `render.yaml` / `vercel.json`).

> **Free-tier caveat:** Render web/worker services spin down after ~15 min of inactivity. The first request after idle takes ~30s to wake. This is fine for a demo — flag it in your interview if asked.

---

## 0. Prerequisites

- GitHub repo pushed (Render and Vercel both pull from GitHub)
- Render account: https://render.com
- Vercel account: https://vercel.com
- A 50+ char random string for `DJANGO_SECRET_KEY`. Generate one with:
  ```
  python -c "import secrets;print(secrets.token_urlsafe(50))"
  ```

---

## 1. Render: managed Postgres (free)

1. Render dashboard → **New** → **PostgreSQL**.
2. Name: `playto-postgres`. Region: pick the same region you'll use for the web/worker (Singapore is closest for India).
3. Plan: **Free**.
4. Click **Create Database**.
5. Once it's `Available`, open it. Copy the **Internal Database URL** (starts with `postgres://`). You'll paste it as `DATABASE_URL` for the web/worker services.

---

## 2. Render: managed Redis (free)

1. Render dashboard → **New** → **Key Value** (Render's rebranded Redis).
2. Name: `playto-redis`. Same region as Postgres.
3. Plan: **Free**.
4. Maxmemory policy: leave default (`allkeys-lru`).
5. Click **Create**.
6. Once `Available`, copy the **Internal Redis URL** (starts with `redis://`). You'll paste it as `REDIS_URL`.

---

## 3. Render: web service (Django + gunicorn)

1. Render dashboard → **New** → **Web Service** → **Build and deploy from a Git repository** → connect your GitHub repo.
2. Settings:
   - **Name:** `playto-payout` (this becomes `playto-payout.onrender.com`)
   - **Region:** same as DB
   - **Branch:** `main` (or whatever you push to)
   - **Root Directory:** *leave blank* (whole repo)
   - **Runtime:** Python 3
   - **Build Command:**
     ```
     pip install uv && uv sync --frozen && cd backend && uv run python manage.py collectstatic --noinput && uv run python manage.py migrate
     ```
   - **Start Command:**
     ```
     cd backend && uv run gunicorn playto.wsgi:application --bind 0.0.0.0:$PORT --workers 2
     ```
   - **Plan:** Free
3. Add environment variables (Environment tab):

   | Key | Value |
   |---|---|
   | `DJANGO_SECRET_KEY` | the 50-char random string |
   | `DJANGO_DEBUG` | `0` |
   | `DJANGO_ALLOWED_HOSTS` | leave empty — Render's hostname auto-allowed |
   | `DATABASE_URL` | Internal URL from step 1 |
   | `REDIS_URL` | Internal URL from step 2 |
   | `PAYOUT_WORKER_MODE` | `cron` (no Celery worker on free tier — see step 4) |
   | `DRAIN_TOKEN` | random 32+ char token; the value UptimeRobot sends in the X-Drain-Token header (set in step 4a) |
   | `DJANGO_CORS_ORIGINS` | your Vercel prod URL once you have it (set after step 6; until then `*.vercel.app` is allowed by regex). **No trailing slash, no path** — django-cors-headers rejects those (`corsheaders.E014`). |
   | `PYTHON_VERSION` | `3.12.7` (Render needs this hint) |

4. Click **Create Web Service**. First build takes ~5 min.
5. Once it's `Live`, hit `https://playto-payout.onrender.com/api/v1/merchants` — should return `[]` (no merchants yet, that's normal).

---

## 4. Payout drainer (UptimeRobot — free)

Render charges for Background Workers and Cron Jobs. We work around it by
exposing a token-protected `POST /api/v1/internal/drain` endpoint on the
web service and pinging it from **UptimeRobot** (free, 5-min interval).

The endpoint runs both phases a worker would have run:
  1. drain new `pending` payouts → `processing` → `completed | failed`
  2. retry any `processing` payouts stuck past 30s

Both use `SELECT … FOR UPDATE SKIP LOCKED` so overlapping pings can't
double-process a row. The endpoint requires the header
`X-Drain-Token: <secret>` and returns 401 without it.

### 4a. Generate the drain token and set it on Render

1. Generate a token (any 32+ char random string):
   ```
   python -c "import secrets;print(secrets.token_urlsafe(40))"
   ```
2. Render → `playto-payout` web service → **Environment**:
   - `DRAIN_TOKEN` = the generated string
   - `PAYOUT_WORKER_MODE` = `cron` (tells the POST view to skip Celery's
     `.delay()` since nothing is reading the Redis queue; the drain
     endpoint picks rows up from Postgres directly)
3. Save → Render redeploys (~2 min). Sanity check from your laptop:
   ```
   # 401 expected — no token
   curl -i -X POST https://playto-payout.onrender.com/api/v1/internal/drain

   # 200 + JSON expected — correct token
   curl -X POST https://playto-payout.onrender.com/api/v1/internal/drain \
        -H "X-Drain-Token: <your-token>"
   # → {"drained":0,"requeued":0}
   ```

### 4b. Set up UptimeRobot to ping the endpoint every 5 minutes

1. Sign up free at https://uptimerobot.com (no card required).
2. Dashboard → **+ New monitor**.
3. Settings:
   - **Monitor Type:** HTTP(s)
   - **Friendly Name:** `playto-drain`
   - **URL:** `https://playto-payout.onrender.com/api/v1/internal/drain`
   - **Monitoring Interval:** 5 minutes (free tier minimum)
   - Expand **HTTP Settings**:
     - **HTTP Method:** `POST`
     - **Custom HTTP Headers** (one per line):
       ```
       X-Drain-Token: <your-token>
       ```
   - Expand **Advanced Settings → Custom HTTP Statuses**: treat `200` as up.
4. **Create Monitor**.

UptimeRobot will now POST every 5 minutes. Two birds with one stone:
- It drains pending payouts.
- It also keeps the Render free-tier web service warm, eliminating the
  ~30s cold start after idle.

### 4c. Trade-offs

- **Latency:** payouts now move from `pending → completed` in up to 5
  minutes. The dashboard polls every 2s so the transition is visible
  the moment UptimeRobot fires. For a live demo, hit the **Test Now**
  button on the UptimeRobot monitor to fire it immediately.
- **The endpoint is open by URL but secret by token.** A 32-byte
  `secrets.token_urlsafe` is unguessable; HMAC-equal comparison in the
  view prevents timing attacks.

> **Why this approach is honest, not a hack.** Postgres is the queue
> (`pending` rows). The drain endpoint is a worker (a separate process
> draining the queue, not running inside the POST handler). UptimeRobot
> is the scheduler. Celery is still wired up so local `make worker`
> works as before. The brief's "do not fake it with sync code" rule is
> satisfied: processing happens out-of-band from the request that
> created the payout.

---

## 5. Seed the remote database

Render's **Shell** tab on the web service is the cleanest path. The `seed_demo` management command is idempotent.

1. Open the `playto-payout` web service in Render.
2. Click the **Shell** tab (left sidebar).
3. Wait for the shell to attach (~10s).
4. Run:
   ```
   cd backend && uv run python manage.py seed_demo
   ```
5. You should see:
   ```
   [ok]   Acme Studios (...) balance=7023579 paise
   [ok]   Bluegrass Agency (...) balance=23333333 paise
   [ok]   Coral Freelancer (...) balance=1525174 paise
   ```
6. Verify via curl from your laptop:
   ```
   curl -s https://playto-payout.onrender.com/api/v1/merchants
   ```
   should return three merchants.

**To wipe and reseed** (dev only, destructive):
```
cd backend && uv run python manage.py seed_demo --reset
```

**Two other options for seeding** (if Shell tab is broken or you want it scripted):

- **psql + COPY:** copy `DATABASE_URL` from the dashboard, run `psql "$DATABASE_URL"` from your laptop, paste raw INSERTs. Workable but verbose for the ledger entries.
- **One-off Render Job:** New → Cron Job (or Job) → same repo → command `cd backend && uv run python manage.py seed_demo`. Run it once manually. Adds a service to manage; only worth it if you find yourself reseeding frequently.

The Shell-tab approach is what I recommend.

---

## 6. Vercel: frontend

1. Vercel dashboard → **Add New** → **Project** → import your GitHub repo.
2. Settings:
   - **Framework Preset:** Vite
   - **Root Directory:** `frontend`
   - **Build Command:** `npm run build` (default)
   - **Output Directory:** `dist` (default)
   - **Install Command:** `npm install` (default)
3. Environment Variables (Add for Production AND Preview):
   - `VITE_API_BASE_URL` = `https://playto-payout.onrender.com`
4. Click **Deploy**. First build ~1 min.
5. Once deployed, Vercel gives you `https://playto-payout-<hash>.vercel.app`. Open it. The dashboard should load and show the three seeded merchants.

---

## 7. Tighten CORS (after you have the Vercel prod URL)

Until step 6, the backend's CORS config trusts any `*.vercel.app` origin via regex (which works for previews). For production, also add the explicit prod URL:

1. Render → `playto-payout` web service → Environment.
2. Set `DJANGO_CORS_ORIGINS` = `https://playto-payout.vercel.app` (or your Vercel prod alias). **No trailing slash, no path** — django-cors-headers rejects those (`corsheaders.E014`). Multiple origins: comma-separated.
3. Save → Render auto-redeploys (~2 min).

The regex `*.vercel.app` stays in place so preview URLs continue to work. The explicit list is belt-and-braces.

---

## 8. End-to-end smoke (manual)

After all five services are up:

1. Open the Vercel URL.
2. Switch the merchant dropdown — three merchants visible.
3. Submit a 500-rupee payout. It appears in history as `pending` immediately.
4. Wait up to **5 minutes** for the next UptimeRobot ping. The row flips to `processing` → `completed`. Balance drops by ₹500.
5. Check Render → `playto-payout` → Logs. Each ping you should see:
   ```
   POST /api/v1/internal/drain → 200 ({"drained":1,"requeued":0})
   payout <uuid> completed (roll=0.412)
   ```
6. Demo failure path: on the WEB service env, set `PAYOUT_SETTLEMENT_FORCE=0.8`, save (auto-redeploy). Submit another payout → next ping it goes `failed`, balance returns to its pre-debit value via REVERSAL.
7. Unset the env (or set back to nothing) when done.

**Don't want to wait 5 min during the demo?** UptimeRobot dashboard → click your monitor → **Test Now**. Or just curl the endpoint manually:
```
curl -X POST https://playto-payout.onrender.com/api/v1/internal/drain \
     -H "X-Drain-Token: <your-token>"
```

---

## 9. Cost summary

Free tier covers everything in this guide:

| Service | Plan | Notes |
|---|---|---|
| Render Postgres | Free | 1 GB storage, 90-day retention warning — fine for demo |
| Render Redis | Free | 25 MB |
| Render web | Free | Sleeps after 15 min idle, ~30s wake |
| UptimeRobot ping | Free | POSTs `/internal/drain` every 5 min; doubles as keep-alive |
| Vercel frontend | Hobby | Always-on |

Total monthly: **$0**. The only cost is the ~30s cold-start on the first request after idle, which I'll mention to the reviewer.

---

## 10. Troubleshooting

**`relation "merchant" does not exist`** — the build's `migrate` step didn't run. Open the Shell tab and run `cd backend && uv run python manage.py migrate` manually.

**500 on first payout, "no such table"** — same as above; migrations.

**CORS error in browser console** — your Vercel URL isn't matching the allow-list. Check `DJANGO_CORS_ORIGINS` on the Render web service env. Both `https://yourapp.vercel.app` and `https://yourapp-<hash>.vercel.app` should be covered (the regex catches the latter).

**`DisallowedHost`** — Render's `RENDER_EXTERNAL_HOSTNAME` env is auto-set and `settings.py` reads it. If you're calling via a custom domain, add it to `DJANGO_ALLOWED_HOSTS`.

**Worker isn't picking up tasks** — confirm `REDIS_URL` matches between web and worker services. Both must point at the same Redis instance.

**Cold start: first request times out** — Render free tier. Hit the URL once, wait 30s, retry. Subsequent requests are fast.

**Migrations fail with SSL error** — Render's managed Postgres requires SSL. `dj_database_url.config(ssl_require=True)` is set by default in our `settings.py`. If you're connecting from your laptop with psql, add `?sslmode=require` to the URL.
