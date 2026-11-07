export async function requestJson(url, options) {
  const response = await fetch(url, options)
  const data = await response.json()
  if (!response.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((item) => item.msg).join('; ')
      : data.detail
    throw new Error(typeof detail === 'string' ? detail : 'Request failed. Please try again.')
  }
  return data
}

export function nextOccurrence(hhmm, from = new Date()) {
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(hhmm) || !Number.isFinite(from.getTime())) {
    throw new Error('Enter a valid time.')
  }
  const formatter = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Europe/London', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  })
  // Search real instants so skipped/repeated clock hours and overseas browsers
  // all use the next occurrence of the requested UK clock time.
  const firstMinute = Math.floor(from.getTime() / 60000) + 1
  for (let minute = firstMinute; minute < firstMinute + 48 * 60; minute += 1) {
    const candidate = new Date(minute * 60000)
    if (formatter.format(candidate) === hhmm) return candidate
  }
  throw new Error('That UK time is unavailable in the next 48 hours.')
}

export function savingText(value, unit, digits = 2) {
  const rounded = Number(value.toFixed(digits))
  if (rounded === 0) return `unchanged ${unit}`
  return `${Math.abs(rounded).toFixed(digits)} ${unit} ${rounded > 0 ? 'less' : 'more'}`
}
