# Day 3 — What was built and how

Day 3 added the React dashboard the brief asks for. The deliberate goal was *minimum viable UI*: enough to demo balance, request a payout, watch its status transition live, and read the ledger — nothing more. The brief is explicit that pixel-perfect UI is not graded, so this is the smallest amount of frontend code that fully exercises the API.

What's new in one sentence: **a single-page React + Vite + Tailwind dashboard that proxies to Django, polls every 2 seconds, generates idempotency keys client-side, and displays balance / held / ledger / payout history with proper status badges.**

---

## 1. What "done" means for Day 3

From the planning doc, Day 3's exit criteria:

- [x] Vite + React + Tailwind scaffold
- [x] Dashboard page: balance, ledger, form, history table, polling
- [x] Wired to API

All three green. End-to-end verified: created a payout from the browser, watched the balance drop, watched the row in the history table flip from `pending` → `completed` within one poll cycle.

---

## 2. Folder structure after Day 3

New and changed files marked with `★`. Files unchanged from earlier days are listed but unmarked.

```
assignment-playtopay/
├── .venv/                                     # Python venv
├── pyproject.toml
├── uv.lock
├── Makefile                                   # ★ frontend-* targets added
├── COMMANDS.md                                # ★ frontend section + 2-terminal flow
├── planning.md
├── DAY1.md
├── DAY2.md
├── DAY3.md                                    # ★ this file
├── backend/                                   # unchanged from Day 2
└── frontend/                                  # ★ NEW DIRECTORY (entire tree)
    ├── package.json                           # ★ Vite + React + TS + Tailwind v4
    ├── package-lock.json
    ├── tsconfig.json / tsconfig.app.json /
    │   tsconfig.node.json
    ├── vite.config.ts                         # ★ tailwind plugin + /api proxy
    ├── eslint.config.js                       # default scaffold
    ├── index.html                             # default Vite shell
    ├── public/
    │   └── vite.svg
    ├── node_modules/                          # gitignored, populated by npm install
    └── src/
        ├── main.tsx                           # React root entry (default Vite)
        ├── App.tsx                            # ★ dashboard (~370 lines)
        ├── api.ts                             # ★ typed API client
        ├── format.ts                          # ★ paise → rupee display helpers
        ├── index.css                          # ★ Tailwind import + base resets
        ├── vite-env.d.ts                      # default
        └── assets/                            # default Vite logo
```

Three source files do all the work: `App.tsx` is the single page, `api.ts` is the typed fetch layer, `format.ts` is render-only money helpers. There is no router, no global state library, no design system, no component library.

---

## 3. File-by-file breakdown of new code

### `frontend/vite.config.ts` — proxy + tailwind plugin

```ts
plugins: [react(), tailwindcss()],
server: {
  port: 5173,
  proxy: {
    '/api': 'http://127.0.0.1:8000',
  },
},
```

Two decisions worth calling out:

**Why a Vite proxy and not CORS on Django.** A proxy means the browser thinks the API is same-origin, so we never had to add `django-cors-headers`, configure allow-lists, or worry about preflights for the `Idempotency-Key` header. The frontend code uses relative URLs like `/api/v1/merchants`, which works identically in dev (Vite proxy), in production behind a reverse proxy, or in a single-process deployment (whitenoise + Django serving the built `dist/`).

**Why Tailwind v4 via `@tailwindcss/vite`** (not v3 + PostCSS). Tailwind v4 is the current major version. The Vite plugin replaces the v3 trio of `tailwind.config.js` + `postcss.config.js` + `npm run build:css`. CSS becomes one line: `@import "tailwindcss"`. Less config, fewer files, less to explain in a code review.

### `frontend/src/index.css` — global CSS

Three lines that matter:

```css
@import "tailwindcss";

html, body, #root { height: 100%; margin: 0; }

body {
  font-family: ui-sans-serif, system-ui, ...;
  background: #f7f8fa;
  color: #111827;
}
```

The `@import` brings in Tailwind's preflight reset and utility classes. The body sets a neutral page background and the system font stack. No custom design tokens, no `:root` variables — Tailwind's defaults are sufficient. The original Vite scaffolded `App.css` was deleted as it conflicts with Tailwind.

### `frontend/src/api.ts` — typed API client

This file is where the API contract lives in TypeScript. Five exported functions and four types:

**Types:**
- `Merchant` — `{ id, name, available_paise }` from the index endpoint.
- `LedgerEntry` — what `merchant_detail.recent_entries` returns. `entry_type` is a literal union of `'CREDIT' | 'DEBIT' | 'REVERSAL'` matching the Day 1 enum.
- `MerchantDetail` — `{ id, name, available_paise, held_paise, recent_entries }` for the dashboard.
- `Payout` — full payout row including `status`, `attempts`, `last_attempt_at`. `PayoutStatus` is a string-literal union matching the Django state machine.

**Functions:**
- `listMerchants()` — `GET /api/v1/merchants`, returns `Merchant[]`.
- `getMerchant(id)` — `GET /api/v1/merchants/<id>`.
- `listPayouts(merchantId)` — `GET /api/v1/merchants/<id>/payouts/list`.
- `createPayout(merchantId, amountPaise, bankAccountId, idempotencyKey)` — the load-bearing one. POSTs with the `Idempotency-Key` header, parses both success and error JSON, and returns a **discriminated union**:

```ts
type CreatePayoutResult =
  | { ok: true;  payout: Payout }
  | { ok: false; status: number; error: string;
      available_paise?: number; requested_paise?: number }
```

The discriminated union forces callers to handle both cases at the type level. The form code in `App.tsx` does `if (res.ok === true) { ... } else { ... }` and TypeScript narrows correctly inside each branch — no `any`, no optional-chaining hacks.

**Why no fetch wrapper / axios / react-query.** The brief explicitly says feature breadth is not graded. Three plain `fetch` calls and one POST is all the network code we need. Pulling in a query library would mean explaining caching policies, retry behavior, and stale-while-revalidate semantics — none of which the brief asks about.

### `frontend/src/format.ts` — display helpers

Pure functions, no React, no state. Three exports:

- `formatPaise(paise: number): string` — paise integer → `₹X,XX,XXX.YY` with Indian numbering (lakhs/crores grouping). Uses `Math.trunc` and string slicing — never converts to a decimal float for display, which would re-introduce the rounding bug the backend works hard to avoid. Math stays in paise; this function only renders.
- `formatTimestamp(iso)` — ISO string → `toLocaleString()`. Returns `'—'` for null.
- `shortId(uuid)` — first 8 hex chars, for display only.

### `frontend/src/App.tsx` — the dashboard

Single component file, broken into small named sub-components for readability. Reading it top to bottom matches how the page renders top to bottom:

#### `App` — the page root

State:
```ts
const [merchants, setMerchants] = useState<Merchant[]>([])
const [selectedId, setSelectedId] = useState<string | null>(null)
const [detail, setDetail] = useState<MerchantDetail | null>(null)
const [payouts, setPayouts] = useState<Payout[]>([])
const [error, setError] = useState<string | null>(null)
```

Two `useEffect` hooks:

**(a) Boot effect** — runs once on mount. Calls `listMerchants()`, sets the dropdown options, and selects the first merchant by default.

**(b) Polling effect** — depends on `selectedId`. On every selection change (and every 2s thereafter), it calls `getMerchant` and `listPayouts` in parallel via `Promise.all`. Uses a `cancelled` flag and `clearInterval` in the cleanup function so a fast-switching user doesn't get cross-merchant data races. Uses `setInterval` not `setTimeout` recursion — simpler, no risk of lost ticks during a slow request.

**Why polling not WebSockets:** the brief explicitly does not ask for it; planning §16 lists it as an out-of-scope guardrail. 2s is the cadence the planning doc specified.

#### `MerchantSwitcher` — the top-right select

Single `<select>` with the merchant list. When merchants haven't loaded yet, shows `"no merchants seeded"`.

#### `BalanceCard` — three-column stat strip

Available paise (large, bold), Held paise (medium), and the merchant's short id (mono, small). Held shows DEBITs whose linked payout is still pending or processing — that field comes from `Merchant.held_paise()` on the backend.

#### `PayoutForm` — the request form

The most behaviorally interesting part of the frontend. Local state:

```ts
const [amountRupees, setAmountRupees]   = useState('')
const [bankAccountId, setBankAccountId] = useState(uuidv4())
const [submitting, setSubmitting]       = useState(false)
const [feedback, setFeedback]           = useState< {kind:'ok',...} | {kind:'err',...} | null >(null)
```

`onSubmit` is the load-bearing function:

1. Validate the rupee amount is positive and finite. Local guard before any network call.
2. Convert rupees → paise with `Math.round(rupees * 100)`. Inputs like `"12.34"` become exactly `1234` paise; floating-point drift is killed at the boundary.
3. Generate a fresh idempotency key per submission via `crypto.randomUUID()`. Each click is a *new* request — replays only happen if the request errors and the user clicks again with the same key, which we deliberately don't do. (A reviewer can manually replay the exact same key with curl to test the byte-identical replay contract.)
4. POST via `createPayout`.
5. Branch on the discriminated union:
   - On success, show "Created payout `<short-id>`" and clear the amount field.
   - On `insufficient_balance`, format the available balance and surface it: `"Insufficient balance. Available: ₹X,XXX.XX"`.
   - On any other error, surface the raw error code.

The "regen" button next to the bank account id is a UX nicety — clicking it generates a new UUID. Not required, but it makes manual demo easier.

The submit button is disabled when (a) the merchant has zero available, (b) a request is in flight, or (c) the amount field is empty. Disabled-state styling uses Tailwind's `disabled:bg-gray-300 disabled:cursor-not-allowed`.

#### `RecentLedger` — last 10 ledger entries

Fed by `MerchantDetail.recent_entries`. Each row:
- A coloured `EntryBadge` (green CREDIT, red DEBIT, amber REVERSAL)
- Timestamp in mono font
- Amount with sign — DEBITs render as `−` and red, CREDIT/REVERSAL as `+` and green

The colour scheme matches the badge so the eye groups them consistently.

#### `PayoutHistory` — table of recent payouts

Five columns: short payout id, created timestamp, amount, attempts count, status pill. The `attempts` column makes the retry behavior visible during demo — when a payout hangs and the sweep retries it, the count goes up.

Status pills (`StatusPill` component):
- `pending` — gray
- `processing` — blue
- `completed` — green
- `failed` — red

When `make dev SETTLEMENT=0.95` (force-hang) is running and the retry sweep is firing, you can watch a row stay in blue `processing` with `attempts` incrementing each cycle.

#### `uuidv4()` — local helper

Bottom of the file. Uses `crypto.randomUUID()` when available (every modern browser) and falls back to a Math.random-based generator. Avoids pulling a dep just for this.

### `frontend/package.json` — dependencies

Runtime:
- `react`, `react-dom` — scaffolded by Vite

Dev:
- `vite`, `@vitejs/plugin-react` — bundler + JSX transform
- `typescript`, `@types/react`, `@types/react-dom` — type checking
- `tailwindcss@^4`, `@tailwindcss/vite@^4` — Tailwind v4 + Vite plugin
- `eslint` and friends — scaffolded, untouched

That's it. **No router, no state library, no UI kit, no axios, no react-query, no formik, no zod**. The deliberate minimalism is the point.

### Updated `Makefile` and `COMMANDS.md`

Three new make targets:
- `make frontend-install` — `npm install` in `frontend/`
- `make frontend-dev` — `npm run dev` (Vite dev server on `:5173` with proxy)
- `make frontend-build` — `npm run build` (production build into `frontend/dist/`)

`COMMANDS.md` updated to the two-terminal workflow:
```
T1: make dev
T2: make frontend-dev
Browser: http://localhost:5173/
```

`make clean` now also removes `frontend/node_modules` and `frontend/dist`.

---

## 4. How the pieces fit together at runtime

```
Browser (http://localhost:5173)
  │
  │  HTML/JS/CSS                ── served by Vite ──┐
  │                                                 │
  │  GET  /api/v1/merchants      ──▶ Vite proxy ──▶ Django :8000 ──▶ Postgres
  │  GET  /api/v1/merchants/<id> ──▶ Vite proxy ──▶ Django :8000 ──▶ Postgres
  │  GET  /api/v1/.../payouts/list──▶ Vite proxy ──▶ Django :8000 ──▶ Postgres
  │  POST /api/v1/.../payouts   ──▶ Vite proxy ──▶ Django :8000 ──▶ Postgres
  │                                                 │           ──▶ Celery (eager) ──▶ Postgres
  │
  │  Every 2s: useEffect timer fires
  │  ── refetches getMerchant + listPayouts in parallel ──
  │  ── React rerenders the affected sections only ──
```

The frontend is purely a thin polling client. Every state change a user sees comes from a re-fetched representation of server state. There is no client-side mutation of cached data — when a payout is submitted, the success response is acknowledged, but the dashboard itself updates from the next poll cycle, which exercises the round-trip and confirms the server actually persisted what we think it did.

---

## 5. End-to-end demo flow (what a reviewer should see)

1. Open `http://localhost:5173/`. Header shows "Playto Payout Dashboard" + a merchant dropdown defaulting to Acme Studios.
2. Balance card shows Acme's available `₹70,235.79`, held `₹0.00`, short id.
3. Recent ledger shows the three seeded CREDIT entries with green badges.
4. Type `500` into the amount field, click "Submit payout".
5. Form clears. Green confirmation appears with the new payout's short id.
6. Within 2 seconds, the polling tick refetches:
   - Available balance drops to `₹69,735.79`.
   - A new red `−₹500.00 DEBIT` entry appears at the top of the ledger.
   - The payout history table gets a new row, status pill `pending`.
7. Within another tick or two, the row moves to `completed` (because eager Celery ran the worker inline, and `PAYOUT_SETTLEMENT_FORCE=0.0` forces success).
8. Switch the dropdown to Bluegrass Agency — the dashboard repaints with that merchant's data.
9. To demo failure: stop both servers, run `make dev SETTLEMENT=0.8`, restart the frontend. Submit a payout. Watch it go pending → processing → failed, and watch the balance restore (because the worker writes a REVERSAL atomically with the failed transition).
10. To demo retry: `make dev SETTLEMENT=0.95`. Submit a payout. Watch attempts climb each ~30s while it stays in `processing`. (Note: retry beat needs a real `make beat` runner; eager mode runs the simulation inline once.)

---

## 6. Tradeoffs and what I deliberately skipped

**No router.** Single page. The merchant switcher is a `<select>`, not a route. URL state is irrelevant for this challenge.

**No client-side validation library.** A `Number.isFinite() && > 0` guard plus a number input with `min/step` is enough. Adding zod for one form would be theatre.

**No optimistic UI.** When you submit, the form waits for the server response, then waits for the next poll. This is honest — it shows the user actual server state, not a hopeful preview. Optimistic updates make sense in social apps; for money, "wait until the server confirmed" is the right default.

**No accessibility audit.** Inputs are labeled, buttons have meaningful text, and Tailwind's defaults pass basic contrast. I did not run axe or VoiceOver.

**No tests on the frontend.** The brief explicitly says "feature breadth beyond what is listed" is not graded. Two graded tests live on the backend (Day 2) where the money correctness lives.

---

## 7. Issues hit and how I fixed them

### Issue 1: TypeScript narrowing on the discriminated union

The first version of `PayoutForm` had:
```ts
if (res.ok) { ... } else { ... res.error ... }
```

The compiler complained `Property 'error' does not exist on type 'CreatePayoutResult'` inside the else. The narrowing was being lost because of the surrounding `try`/`catch`/`finally` block — TS's control-flow analyzer doesn't always track the discriminant through async setState calls.

Fix: `if (res.ok === true) { ... } else { const fail = res; ... fail.error ... }`. Binding to a fresh `const` inside the else lets the analyzer keep the narrowed type.

### Issue 2: Vite v8 binds to `localhost`, not `127.0.0.1`

A curl test against `127.0.0.1:5173` failed; `localhost:5173` worked. Vite's default host changed between versions and now binds only to the loopback hostname, not the IPv4 address. Documented in `COMMANDS.md` to use `http://localhost:5173/`. (For LAN access you'd add `--host 0.0.0.0`, but that's outside the demo.)

---

## 8. What's NOT in Day 3 (intentionally)

- ❌ Authentication / login screen (planning §16)
- ❌ Multi-merchant tabs or routes
- ❌ Realtime WebSockets (planning §16)
- ❌ Date pickers, filters, pagination on the history table
- ❌ Optimistic mutations
- ❌ Frontend tests
- ❌ Production deploy of the frontend (Day 4 — built by `make frontend-build` and served by Django/whitenoise from `dist/`)

---

## 9. How to verify Day 3 yourself

```bash
# One-time
make install
make migrate
make seed
make frontend-install

# Two terminals
make dev                # T1: backend on :8000
make frontend-dev       # T2: frontend on :5173

# Browser
open http://localhost:5173/

# Optional: production build sanity
make frontend-build     # writes frontend/dist/
```

Expected: dashboard loads, three merchants in dropdown, balance card populated, ledger shows seeded credits. Submit a 500 rupee payout — it appears in history and completes within ~4 seconds.

---

## 10. Day 3 in one sentence

A 370-line single-component React dashboard with a typed API client and a paise-aware money formatter, wired to the Day 2 backend via a Vite proxy, polling every 2 seconds — showing balance, ledger, the request form, and live payout status — with no router, no state library, and no UI kit beyond Tailwind v4.

Day 4 is deploy + EXPLAINER.md + README.
