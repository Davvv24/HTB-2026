import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

let server
before(async () => {
  server = await createServer({ server: { middlewareMode: true, watch: null, ws: false } })
})
after(async () => { await server?.close() })

test('the explanation remains visible without model contributions', async () => {
  const { default: WhyPanel } = await server.ssrLoadModule('/src/components/WhyPanel.jsx')
  const html = renderToStaticMarkup(createElement(WhyPanel, { why: 'This slot costs less than starting now.' }))
  assert.match(html, /This slot costs less than starting now/)
})

test('the grid heartbeat tolerates missing and empty mixes and labels replay', async () => {
  const { default: GridHeartbeat } = await server.ssrLoadModule('/src/components/GridHeartbeat.jsx')
  for (const mix_percent of [undefined, {}, { gas: 0, wind: null }]) {
    const html = renderToStaticMarkup(createElement(GridHeartbeat, { grid: {
      data_as_of: '2026-11-03T15:00:00Z', carbon_intensity: 150,
      historical_demo: true, mix_percent,
    } }))
    assert.match(html, /Generation mix unavailable/)
    assert.match(html, /Historical replay/)
    assert.doesNotMatch(html, /NaN|undefined/)
  }
})
