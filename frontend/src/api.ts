/**
 * API client. Vite proxies `/api` to Django, so we use a relative path
 * here and don't deal with CORS or absolute URLs.
 */

export type Merchant = {
  id: string
  name: string
  available_paise: number
}

export type LedgerEntry = {
  id: string
  entry_type: 'CREDIT' | 'DEBIT' | 'REVERSAL'
  amount_paise: number
  payout_id: string | null
  created_at: string
}

export type MerchantDetail = {
  id: string
  name: string
  available_paise: number
  held_paise: number
  recent_entries: LedgerEntry[]
}

export type PayoutStatus = 'pending' | 'processing' | 'completed' | 'failed'

export type Payout = {
  id: string
  merchant: string
  amount_paise: number
  bank_account_id: string
  status: PayoutStatus
  attempts: number
  last_attempt_at: string | null
  created_at: string
  updated_at: string
}

const BASE = '/api/v1'

export async function listMerchants(): Promise<Merchant[]> {
  const r = await fetch(`${BASE}/merchants`)
  if (!r.ok) throw new Error(`listMerchants ${r.status}`)
  return r.json()
}

export async function getMerchant(id: string): Promise<MerchantDetail> {
  const r = await fetch(`${BASE}/merchants/${id}`)
  if (!r.ok) throw new Error(`getMerchant ${r.status}`)
  return r.json()
}

export async function listPayouts(merchantId: string): Promise<Payout[]> {
  const r = await fetch(`${BASE}/merchants/${merchantId}/payouts/list`)
  if (!r.ok) throw new Error(`listPayouts ${r.status}`)
  return r.json()
}

export type CreatePayoutResult =
  | { ok: true; payout: Payout }
  | { ok: false; status: number; error: string; available_paise?: number; requested_paise?: number }

export async function createPayout(
  merchantId: string,
  amountPaise: number,
  bankAccountId: string,
  idempotencyKey: string,
): Promise<CreatePayoutResult> {
  const r = await fetch(`${BASE}/merchants/${merchantId}/payouts`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': idempotencyKey,
    },
    body: JSON.stringify({
      amount_paise: amountPaise,
      bank_account_id: bankAccountId,
    }),
  })
  const body = await r.json().catch(() => ({}))
  if (r.ok) return { ok: true, payout: body as Payout }
  return {
    ok: false,
    status: r.status,
    error: (body as { error?: string }).error ?? `http_${r.status}`,
    available_paise: (body as { available_paise?: number }).available_paise,
    requested_paise: (body as { requested_paise?: number }).requested_paise,
  }
}
