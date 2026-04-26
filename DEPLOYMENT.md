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
   | `DJANGO_CORS_ORIGINS` | your Vercel prod URL once you have it (set after step 5; until then `*.vercel.app` is allowed by regex) |
   | `PYTHON_VERSION` | `3.12.7` (Render needs this hint) |

4. Click **Create Web Service**. First build takes ~5 min.
5. Once it's `Live`, hit `https://playto-payout.onrender.com/api/v1/merchants` — should return `[]` (no merchants yet, that's normal).

---

## 4. Render: Celery worker

1. Render dashboard → **New** → **Background Worker** → same repo.
2. Settings:
   - **Name:** `playto-worker`
   - **Region:** same
   - **Build Command:** same as web service
   - **Start Command:**
     ```
     cd backend && uv run celery -A playto worker --loglevel=info --concurrency=2
     ```
   - **Plan:** Free
3. Environment: **copy the same env vars from the web service** — `DJANGO_SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, `PYTHON_VERSION`. (CORS isn't needed on the worker.)
4. Create.

> **Optional — beat scheduler.** For the retry sweep to fire automatically every 10s in production, add a third Background Worker named `playto-beat` with start command `cd backend && uv run celery -A playto beat --loglevel=info`. Same env. **You don't need this for the demo** — `process_payout` runs on submission, and a stuck payout can be re-enqueued manually from the Shell tab. Skip if you want to stay on the free 1-worker quota.

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
2. Set `DJANGO_CORS_ORIGINS` = `https://playto-payout.vercel.app` (or whatever your Vercel prod alias is).
3. Save → Render auto-redeploys (~2 min).

The regex `*.vercel.app` stays in place so preview URLs continue to work. The explicit list is belt-and-braces.

---

## 8. End-to-end smoke (manual)

After all five services are up:

1. Open the Vercel URL.
2. Switch the merchant dropdown — three merchants visible.
3. Submit a 500-rupee payout.
4. Within ~5s the payout history shows it as `pending`, then `processing`, then `completed`. Balance drops by ₹500.
5. Check the worker logs in Render → `playto-worker` → Logs. You should see lines like:
   ```
   payout <uuid> completed (roll=0.412)
   ```
6. Demo failure path: in `playto-worker` env, set `PAYOUT_SETTLEMENT_FORCE=0.8`, save (auto-redeploy). Submit another payout → it goes `failed`, balance returns to its pre-debit value via REVERSAL.
7. Unset the env (or set back to nothing) when done.

---

## 9. Cost summary

Free tier covers everything in this guide:

| Service | Plan | Notes |
|---|---|---|
| Render Postgres | Free | 1 GB storage, 90-day retention warning — fine for demo |
| Render Redis | Free | 25 MB |
| Render web | Free | Sleeps after 15 min idle, ~30s wake |
| Render worker | Free | Same sleep behavior |
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
