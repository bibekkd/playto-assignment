/**
 * Display helpers. Math always stays in paise; these are render-only.
 */

export function formatPaise(paise: number): string {
  // ₹X,YYY.ZZ — Indian grouping, integer paise → display rupees.paise.
  const sign = paise < 0 ? '-' : ''
  const abs = Math.abs(paise)
  const rupees = Math.trunc(abs / 100)
  const remainder = String(abs % 100).padStart(2, '0')
  // Indian numbering: last 3 digits, then groups of 2.
  const s = String(rupees)
  let head = s
  let tail = ''
  if (s.length > 3) {
    head = s.slice(0, -3)
    tail = ',' + s.slice(-3)
    head = head.replace(/\B(?=(\d{2})+(?!\d))/g, ',')
  }
  return `${sign}₹${head}${tail}.${remainder}`
}

export function formatTimestamp(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleString()
}

export function shortId(uuid: string): string {
  return uuid.slice(0, 8)
}
