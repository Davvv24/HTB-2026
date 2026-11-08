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

const UK_PARTS = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Europe/London', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
})

function ukParts(date) {
  const p = Object.fromEntries(UK_PARTS.formatToParts(date).map((part) => [part.type, part.value]))
  return { date: `${p.year}-${p.month}-${p.day}`, time: `${p.hour}:${p.minute}` }
}

// The UK calendar date of an instant, as 'YYYY-MM-DD'
export function ukDate(date) {
  return ukParts(date).date
}

// "Today 09:00", "Tomorrow 07:00" or "Tue 09:00", in UK time relative to `now`
export function choiceLabel(date, now) {
  const { date: day, time } = ukParts(date)
  const [year, month, dayOfMonth] = ukDate(now).split('-').map(Number)
  const tomorrow = new Date(Date.UTC(year, month - 1, dayOfMonth + 1, 12)).toISOString().slice(0, 10)
  if (day === ukDate(now)) return `Today ${time}`
  if (day === tomorrow) return `Tomorrow ${time}`
  const weekday = date.toLocaleDateString('en-GB', { weekday: 'short', timeZone: 'Europe/London' })
  return `${weekday} ${time}`
}

// Times on the hour after `from`, up to and including `until`, for a dropdown.
// UK time is always a whole number of hours from UTC, so UTC hours are UK hours.
export function timeChoices(from, until, now = from) {
  const hour = 3600 * 1000
  const choices = []
  for (let t = Math.floor(from.getTime() / hour) * hour + hour; t <= until.getTime(); t += hour) {
    const date = new Date(t)
    choices.push({ value: date.toISOString(), label: choiceLabel(date, now) })
  }
  return choices
}

// The default finish: the next 07:00 if it is in the list, else the latest choice
export function defaultFinish(choices, start) {
  if (choices.length === 0) return ''
  const seven = nextOccurrence('07:00', start).toISOString()
  return choices.some((c) => c.value === seven) ? seven : choices.at(-1).value
}

// The chosen start ('' = now) and finish as exact instants, or an explanation
export function planWindow(now, startValue, finishValue) {
  const start = startValue ? new Date(startValue) : now
  if (start < now) {
    throw new Error('That start time has already passed. Pick a later one.')
  }
  const deadline = new Date(finishValue)
  if (!(deadline > start)) {
    throw new Error('The finish time needs to be after the start time.')
  }
  return { start, deadline }
}

// Split hourly choices into Today and Tomorrow (UK dates), labelled with just the time.
// Later days are left out: the forecast reaches about 48 hours ahead.
export function choicesByDay(choices, now) {
  const [year, month, day] = ukDate(now).split('-').map(Number)
  const tomorrow = new Date(Date.UTC(year, month - 1, day + 1, 12)).toISOString().slice(0, 10)
  const byDay = { today: [], tomorrow: [] }
  for (const choice of choices) {
    const { date, time } = ukParts(new Date(choice.value))
    if (date === ukDate(now)) byDay.today.push({ value: choice.value, label: time })
    else if (date === tomorrow) byDay.tomorrow.push({ value: choice.value, label: time })
  }
  return byDay
}
