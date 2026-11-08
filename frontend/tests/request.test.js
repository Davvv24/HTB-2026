import assert from 'node:assert/strict'
import test from 'node:test'
import {
  choiceLabel, choicesByDay, defaultFinish, nextOccurrence, planWindow, requestJson, savingText,
  timeChoices,
} from '../src/request.js'

test('UK deadline uses summer time independently of browser timezone', () => {
  assert.equal(nextOccurrence('07:00', new Date('2026-07-01T15:00:00Z')).toISOString(),
    '2026-07-02T06:00:00.000Z')
})

test('repeated autumn hour picks the next actual occurrence', () => {
  assert.equal(nextOccurrence('01:30', new Date('2026-10-25T00:45:00Z')).toISOString(),
    '2026-10-25T01:30:00.000Z')
})

test('spring skipped hour uses next available day', () => {
  assert.equal(nextOccurrence('01:30', new Date('2026-03-29T00:45:00Z')).toISOString(),
    '2026-03-30T00:30:00.000Z')
})

test('negative savings are labelled more', () => {
  assert.equal(savingText(-0.42, 'GBP'), '0.42 GBP more')
  assert.equal(savingText(0, 'GBP'), 'unchanged GBP')
})

test('structured validation errors become displayable messages', async (context) => {
  context.mock.method(globalThis, 'fetch', async () => ({
    ok: false, json: async () => ({ detail: [{ msg: 'Energy must be positive' }] }),
  }))
  await assert.rejects(requestJson('/api/recommend'), /Energy must be positive/)
})

test('choices are labelled Today, Tomorrow or the weekday in UK time', () => {
  const now = new Date('2026-11-07T15:20:00Z') // Sat 15:20 UK
  assert.equal(choiceLabel(new Date('2026-11-07T20:00:00Z'), now), 'Today 20:00')
  assert.equal(choiceLabel(new Date('2026-11-08T07:00:00Z'), now), 'Tomorrow 07:00')
  assert.equal(choiceLabel(new Date('2026-11-09T09:00:00Z'), now), 'Mon 09:00')
  // Summer time: 23:30 UTC on 7 Jul is already 8 Jul 00:30 in the UK
  const summer = new Date('2026-07-07T22:10:00Z') // Tue 23:10 UK
  assert.equal(choiceLabel(new Date('2026-07-07T23:00:00Z'), summer), 'Tomorrow 00:00')
})

test('hourly choices start after now and stop at the end of the forecast', () => {
  const now = new Date('2026-11-07T15:20:00Z')
  const choices = timeChoices(now, new Date('2026-11-09T15:00:00Z'))
  assert.equal(choices[0].label, 'Today 16:00')
  assert.equal(choices[0].value, '2026-11-07T16:00:00.000Z')
  assert.equal(choices.at(-1).label, 'Mon 15:00')
  assert.equal(choices.length, 48)
  // Labels stay relative to now even when listing from a later start
  const later = timeChoices(new Date('2026-11-08T05:00:00Z'), new Date('2026-11-08T08:00:00Z'), now)
  assert.deepEqual(later.map((c) => c.label), ['Tomorrow 06:00', 'Tomorrow 07:00', 'Tomorrow 08:00'])
})

test('the default finish is the next 07:00, or the latest time available', () => {
  const now = new Date('2026-11-07T15:20:00Z')
  const choices = timeChoices(now, new Date('2026-11-09T15:00:00Z'))
  assert.equal(defaultFinish(choices, now), '2026-11-08T07:00:00.000Z')
  const short = timeChoices(now, new Date('2026-11-07T23:00:00Z'))
  assert.equal(defaultFinish(short, now), '2026-11-07T23:00:00.000Z')
  assert.equal(defaultFinish([], now), '')
})

test('the plan window uses now when no start is chosen and checks the order', () => {
  const now = new Date('2026-11-07T15:20:00Z')
  let w = planWindow(now, '', '2026-11-08T07:00:00.000Z')
  assert.equal(w.start, now)
  assert.equal(w.deadline.toISOString(), '2026-11-08T07:00:00.000Z')
  w = planWindow(now, '2026-11-08T16:00:00.000Z', '2026-11-08T21:00:00.000Z')
  assert.equal(w.start.toISOString(), '2026-11-08T16:00:00.000Z')
  assert.throws(() => planWindow(now, '2026-11-07T09:00:00.000Z', '2026-11-08T07:00:00.000Z'), /already passed/)
  assert.throws(() => planWindow(now, '2026-11-08T16:00:00.000Z', '2026-11-08T07:00:00.000Z'), /after the start/)
})

test('choices split into Today and Tomorrow, leaving later days out', () => {
  const now = new Date('2026-11-07T21:20:00Z') // Sat 21:20 UK
  const byDay = choicesByDay(timeChoices(now, new Date('2026-11-09T15:00:00Z')), now)
  assert.deepEqual(byDay.today.map((c) => c.label), ['22:00', '23:00'])
  assert.equal(byDay.tomorrow.length, 24)
  assert.equal(byDay.tomorrow[0].label, '00:00')
  assert.equal(byDay.tomorrow[7].value, '2026-11-08T07:00:00.000Z')
  // Late at night with a short forecast, tomorrow can be empty
  const short = choicesByDay(timeChoices(now, new Date('2026-11-07T23:00:00Z')), now)
  assert.deepEqual(short.tomorrow, [])
})
