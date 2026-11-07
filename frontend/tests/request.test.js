import assert from 'node:assert/strict'
import test from 'node:test'
import { nextOccurrence, requestJson, savingText } from '../src/request.js'

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
