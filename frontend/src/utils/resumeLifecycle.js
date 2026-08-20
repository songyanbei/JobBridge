/** Parse API lifecycle timestamps. MySQL DATETIME(6) values have no offset and are UTC. */
export function parseUtcTimestamp(value) {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
  if (typeof value === 'number') {
    const parsed = new Date(value)
    return Number.isNaN(parsed.getTime()) ? null : parsed
  }
  if (typeof value !== 'string' || !value.trim()) return null

  // JavaScript dates are millisecond-precision; normalize DATETIME(6)'s
  // fractional part explicitly instead of relying on browser-specific parsing.
  const text = value.trim().replace(' ', 'T').replace(/\.(\d{3})\d+/, '.$1')
  const hasOffset = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text)
  const parsed = new Date(hasOffset ? text : `${text}Z`)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

export function resolveResumeLifecycle(resume, now = new Date()) {
  if (!resume || resume.deleted_at || resume.delist_reason) return 'history'

  const current = parseUtcTimestamp(now)
  if (!current) throw new TypeError('now must be a valid timestamp')

  if (resume.activated_at) {
    const expiresAt = parseUtcTimestamp(resume.expires_at)
    if (!expiresAt) return 'legacy'
    return expiresAt <= current ? 'history' : 'active'
  }

  const candidateExpiresAt = parseUtcTimestamp(resume.candidate_expires_at)
  if (candidateExpiresAt) {
    return candidateExpiresAt <= current ? 'history' : 'candidate'
  }
  return 'legacy'
}
