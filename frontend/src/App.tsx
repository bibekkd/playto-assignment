import { useEffect, useState } from 'react'
import {
  createPayout,
  getMerchant,
  listMerchants,
  listPayouts,
  type Merchant,
  type MerchantDetail,
  type Payout,
} from './api'
import { formatPaise, formatTimestamp, shortId } from './format'

const POLL_MS = 2000

export default function App() {
  const [merchants, setMerchants] = useState<Merchant[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<MerchantDetail | null>(null)
  const [payouts, setPayouts] = useState<Payout[]>([])
  const [error, setError] = useState<string | null>(null)

  // Boot: load merchant list once.
  useEffect(() => {
    listMerchants()
      .then((ms) => {
        setMerchants(ms)
        if (ms.length > 0) setSelectedId(ms[0].id)
      })
      .catch((e) => setError(String(e)))
  }, [])

  // Poll every 2s for the selected merchant.
  useEffect(() => {
    if (!selectedId) return
    let cancelled = false
    const tick = async () => {
      try {
        const [d, p] = await Promise.all([
          getMerchant(selectedId),
          listPayouts(selectedId),
        ])
        if (!cancelled) {
          setDetail(d)
          setPayouts(p)
        }
      } catch (e) {
        if (!cancelled) setError(String(e))
      }
    }
    tick()
    const id = setInterval(tick, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [selectedId])

  return (
    <div className="min-h-screen">
      <header className="border-b border-gray-200 bg-white">
        <div className="mx-auto max-w-5xl px-6 py-4 flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold text-gray-900">
              Playto Payout Dashboard
            </h1>
            <p className="text-sm text-gray-500">
              Merchant balance, ledger, and payout history
            </p>
          </div>
          <MerchantSwitcher
            merchants={merchants}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-6 py-8 space-y-6">
        {error && (
          <div className="bg-red-50 border border-red-200 text-red-700 rounded p-3 text-sm">
            {error}
          </div>
        )}

        {detail && (
          <>
            <BalanceCard detail={detail} />
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              <PayoutForm
                merchantId={detail.id}
                disabled={detail.available_paise <= 0}
              />
              <RecentLedger detail={detail} />
            </div>
            <PayoutHistory payouts={payouts} />
          </>
        )}
      </main>
    </div>
  )
}

function MerchantSwitcher({
  merchants,
  selectedId,
  onSelect,
}: {
  merchants: Merchant[]
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  if (merchants.length === 0) {
    return <span className="text-sm text-gray-500">no merchants seeded</span>
  }
  return (
    <select
      value={selectedId ?? ''}
      onChange={(e) => onSelect(e.target.value)}
      className="border border-gray-300 rounded-md px-3 py-2 text-sm bg-white"
    >
      {merchants.map((m) => (
        <option key={m.id} value={m.id}>
          {m.name}
        </option>
      ))}
    </select>
  )
}

function BalanceCard({ detail }: { detail: MerchantDetail }) {
  return (
    <section className="bg-white rounded-lg border border-gray-200 p-6 grid grid-cols-1 sm:grid-cols-3 gap-4">
      <Stat label="Available" value={formatPaise(detail.available_paise)} accent />
      <Stat label="Held (in flight)" value={formatPaise(detail.held_paise)} />
      <Stat label="Merchant ID" value={shortId(detail.id)} mono small />
    </section>
  )
}

function Stat({
  label,
  value,
  accent,
  mono,
  small,
}: {
  label: string
  value: string
  accent?: boolean
  mono?: boolean
  small?: boolean
}) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wider text-gray-500">{label}</div>
      <div
        className={[
          accent ? 'text-2xl text-gray-900' : 'text-xl text-gray-700',
          small ? 'text-sm text-gray-700' : '',
          mono ? 'font-mono' : 'font-semibold',
        ].join(' ')}
      >
        {value}
      </div>
    </div>
  )
}

function PayoutForm({
  merchantId,
  disabled,
}: {
  merchantId: string
  disabled: boolean
}) {
  const [amountRupees, setAmountRupees] = useState('')
  const [bankAccountId, setBankAccountId] = useState(uuidv4())
  const [submitting, setSubmitting] = useState(false)
  const [feedback, setFeedback] = useState<
    | { kind: 'ok'; payoutId: string }
    | { kind: 'err'; msg: string }
    | null
  >(null)

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    const rupees = Number(amountRupees)
    if (!Number.isFinite(rupees) || rupees <= 0) {
      setFeedback({ kind: 'err', msg: 'Enter a positive rupee amount' })
      return
    }
    // Convert rupees → paise as an integer. Math.round protects against
    // floating-point drift on inputs like "12.34".
    const paise = Math.round(rupees * 100)
    const idempotencyKey = uuidv4()

    setSubmitting(true)
    setFeedback(null)
    try {
      const res = await createPayout(merchantId, paise, bankAccountId, idempotencyKey)
      if (res.ok === true) {
        setFeedback({ kind: 'ok', payoutId: res.payout.id })
        setAmountRupees('')
      } else {
        const fail = res
        let msg: string = fail.error
        if (fail.error === 'insufficient_balance' && fail.available_paise != null) {
          msg = `Insufficient balance. Available: ${formatPaise(fail.available_paise)}`
        }
        setFeedback({ kind: 'err', msg })
      }
    } catch (e) {
      setFeedback({ kind: 'err', msg: String(e) })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="bg-white rounded-lg border border-gray-200 p-6">
      <h2 className="font-semibold text-gray-900 mb-4">Request payout</h2>
      <form onSubmit={onSubmit} className="space-y-4">
        <Field label="Amount (rupees)">
          <input
            type="number"
            min="0.01"
            step="0.01"
            value={amountRupees}
            onChange={(e) => setAmountRupees(e.target.value)}
            disabled={disabled || submitting}
            placeholder="500.00"
            className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm font-mono"
          />
        </Field>
        <Field label="Bank account id">
          <div className="flex gap-2">
            <input
              type="text"
              value={bankAccountId}
              onChange={(e) => setBankAccountId(e.target.value)}
              className="flex-1 border border-gray-300 rounded-md px-3 py-2 text-xs font-mono"
            />
            <button
              type="button"
              onClick={() => setBankAccountId(uuidv4())}
              className="text-xs px-2 py-1 border border-gray-300 rounded-md hover:bg-gray-50"
            >
              regen
            </button>
          </div>
        </Field>
        <button
          type="submit"
          disabled={disabled || submitting || amountRupees === ''}
          className="w-full bg-gray-900 text-white rounded-md px-4 py-2 text-sm font-medium hover:bg-gray-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
        >
          {submitting ? 'Submitting…' : 'Submit payout'}
        </button>
        {feedback?.kind === 'ok' && (
          <div className="text-sm text-green-700 bg-green-50 border border-green-200 rounded p-2">
            Created payout <span className="font-mono">{shortId(feedback.payoutId)}</span>
          </div>
        )}
        {feedback?.kind === 'err' && (
          <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded p-2">
            {feedback.msg}
          </div>
        )}
      </form>
    </section>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-xs uppercase tracking-wider text-gray-500 mb-1">
        {label}
      </label>
      {children}
    </div>
  )
}

function RecentLedger({ detail }: { detail: MerchantDetail }) {
  return (
    <section className="bg-white rounded-lg border border-gray-200 p-6">
      <h2 className="font-semibold text-gray-900 mb-4">Recent ledger entries</h2>
      {detail.recent_entries.length === 0 ? (
        <p className="text-sm text-gray-500">No entries yet.</p>
      ) : (
        <ul className="divide-y divide-gray-100">
          {detail.recent_entries.map((e) => (
            <li key={e.id} className="py-2 flex items-center justify-between">
              <div className="flex items-center gap-3">
                <EntryBadge type={e.entry_type} />
                <span className="text-xs text-gray-500 font-mono">
                  {formatTimestamp(e.created_at)}
                </span>
              </div>
              <span
                className={[
                  'font-mono text-sm',
                  e.entry_type === 'DEBIT' ? 'text-red-700' : 'text-green-700',
                ].join(' ')}
              >
                {e.entry_type === 'DEBIT' ? '−' : '+'}
                {formatPaise(e.amount_paise)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function EntryBadge({ type }: { type: 'CREDIT' | 'DEBIT' | 'REVERSAL' }) {
  const styles: Record<string, string> = {
    CREDIT: 'bg-green-100 text-green-800',
    DEBIT: 'bg-red-100 text-red-800',
    REVERSAL: 'bg-amber-100 text-amber-800',
  }
  return (
    <span className={`text-xs font-semibold px-2 py-0.5 rounded ${styles[type]}`}>
      {type}
    </span>
  )
}

function PayoutHistory({ payouts }: { payouts: Payout[] }) {
  return (
    <section className="bg-white rounded-lg border border-gray-200">
      <div className="px-6 py-4 border-b border-gray-200">
        <h2 className="font-semibold text-gray-900">Payout history</h2>
      </div>
      {payouts.length === 0 ? (
        <p className="px-6 py-4 text-sm text-gray-500">No payouts yet.</p>
      ) : (
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-xs uppercase tracking-wider text-gray-500">
            <tr>
              <th className="text-left px-6 py-2">Payout</th>
              <th className="text-left px-6 py-2">Created</th>
              <th className="text-right px-6 py-2">Amount</th>
              <th className="text-center px-6 py-2">Attempts</th>
              <th className="text-center px-6 py-2">Status</th>
            </tr>
          </thead>
          <tbody>
            {payouts.map((p) => (
              <tr key={p.id} className="border-t border-gray-100">
                <td className="px-6 py-2 font-mono text-xs">{shortId(p.id)}</td>
                <td className="px-6 py-2 text-xs text-gray-500">
                  {formatTimestamp(p.created_at)}
                </td>
                <td className="px-6 py-2 text-right font-mono">
                  {formatPaise(p.amount_paise)}
                </td>
                <td className="px-6 py-2 text-center">{p.attempts}</td>
                <td className="px-6 py-2 text-center">
                  <StatusPill status={p.status} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

function StatusPill({ status }: { status: Payout['status'] }) {
  const styles: Record<Payout['status'], string> = {
    pending: 'bg-gray-100 text-gray-700',
    processing: 'bg-blue-100 text-blue-700',
    completed: 'bg-green-100 text-green-800',
    failed: 'bg-red-100 text-red-700',
  }
  return (
    <span className={`text-xs font-semibold px-2 py-0.5 rounded ${styles[status]}`}>
      {status}
    </span>
  )
}

// Lightweight UUID v4 — avoids pulling in a dep. Uses crypto.randomUUID in
// modern browsers; falls back to Math.random for older ones.
function uuidv4(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID()
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}
