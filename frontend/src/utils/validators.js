export function isValidJson(text) {
  if (typeof text !== 'string' || !text.trim()) return false
  try {
    JSON.parse(text)
    return true
  } catch (_e) {
    return false
  }
}

export function parseJsonSafe(text) {
  try {
    return { ok: true, value: JSON.parse(text) }
  } catch (e) {
    return { ok: false, error: e.message }
  }
}

export function isPhone(value) {
  return /^1[3-9]\d{9}$/.test(String(value || '').trim())
}

export function rangeDays(from, to) {
  if (!from || !to) return null
  const f = new Date(from)
  const t = new Date(to)
  if (Number.isNaN(f.getTime()) || Number.isNaN(t.getTime())) return null
  return Math.ceil((t.getTime() - f.getTime()) / 86400000)
}
