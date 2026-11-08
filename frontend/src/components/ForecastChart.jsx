import createPlotlyComponent from 'react-plotly.js/factory'
import Plotly from 'plotly.js-dist-min'

const Plot = createPlotlyComponent(Plotly)

// UTC timestamp -> "2026-11-07 20:30:00" in UK time, a format Plotly reads as a date
function londonTime(iso) {
  return new Date(iso).toLocaleString('sv-SE', { timeZone: 'Europe/London' })
}

// Green = lowest third of the next 48h, amber = middle, red = highest third
function levelColours(values) {
  const sorted = [...values].sort((a, b) => a - b)
  const low = sorted[Math.floor(sorted.length / 3)]
  const high = sorted[Math.floor((2 * sorted.length) / 3)]
  return values.map((v) => (v <= low ? '#1a7f4b' : v >= high ? '#c0392b' : '#b7791f'))
}

export default function ForecastChart({
  slots, metric, unit, title, rec, confirmedUntil, windowStart, windowEnd,
}) {
  const p10 = slots.map((s) => s[`${metric}_p10`])
  const p50 = slots.map((s) => s[`${metric}_p50`])
  const p90 = slots.map((s) => s[`${metric}_p90`])

  // Plotly centres a bar on its x value, so place each half-hour's bar at the
  // middle of its slot. Bars then span their real time, and the recommended
  // window's edges (at slot boundaries) fall in the gaps between bars.
  const middles = slots.map((s) => londonTime(new Date(new Date(s.time).getTime() + 15 * 60 * 1000)))
  const slotLabels = slots.map((s) => new Date(s.time).toLocaleString('en-GB', {
    weekday: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'Europe/London',
  }))

  const traces = [
    {
      // Bars have three colours, so they get their own legend entries below
      x: middles, y: p50, type: 'bar', name: 'Most likely', showlegend: false,
      marker: { color: levelColours(p50) },
      customdata: slotLabels,
      hovertemplate: `%{customdata}: %{y:.1f} ${unit}<extra></extra>`,
    },
    // Invisible top edge of the band...
    { x: middles, y: p90, type: 'scatter', mode: 'lines', line: { width: 0 },
      hoverinfo: 'skip', showlegend: false },
    // ...and the bottom edge, filled up to the top edge = the 80% range
    { x: middles, y: p10, type: 'scatter', mode: 'lines', line: { width: 0 },
      fill: 'tonexty', fillcolor: 'rgba(29, 29, 31, 0.15)',
      name: 'Likely range', hoverinfo: 'skip' },
  ]

  // Legend-only entries (no data) explaining each colour on the chart
  const words = metric === 'carbon' ? ['Cleanest', 'Dirtiest'] : ['Cheapest', 'Priciest']
  const legendSwatch = (name, color, line) => ({
    x: [null], y: [null], type: 'scatter', mode: 'markers', name, hoverinfo: 'skip',
    marker: { symbol: 'square', size: 12, color, line: line ?? { width: 0 } },
  })
  traces.push(
    legendSwatch(`${words[0]} third`, '#1a7f4b'),
    legendSwatch('Middle third', '#b7791f'),
    legendSwatch(`${words[1]} third`, '#c0392b'),
  )
  if (rec) {
    traces.push(legendSwatch('Recommended time', 'rgba(26, 127, 75, 0.12)', { color: '#1a7f4b', width: 1.5 }))
  }

  const shapes = []
  const outlines = []
  // Shade the recommended window, once there is a recommendation. The fill sits
  // behind the bars; the outline is drawn last, above everything, so no bar or
  // other line can hide part of it.
  for (const block of rec?.blocks ?? (rec ? [{ start: rec.start, end: rec.end }] : [])) {
    const edges = {
      type: 'rect', xref: 'x', yref: 'paper',
      x0: londonTime(block.start), x1: londonTime(block.end), y0: 0, y1: 1,
    }
    shapes.push({ ...edges, fillcolor: 'rgba(26, 127, 75, 0.12)', line: { width: 0 }, layer: 'below' })
    outlines.push({ ...edges, fillcolor: 'rgba(0, 0, 0, 0)', line: { color: '#1a7f4b', width: 1.5 }, layer: 'above' })
  }
  const annotations = []
  // Dashed line where confirmed prices end and our forecast begins
  if (confirmedUntil && metric === 'price') {
    shapes.push({
      type: 'line', xref: 'x', yref: 'paper',
      x0: londonTime(confirmedUntil), x1: londonTime(confirmedUntil), y0: 0, y1: 1,
      line: { color: '#6b6b70', width: 1, dash: 'dash' },
    })
  }

  // Solid lines at the start and finish the user has chosen
  for (const [when, label] of [[windowStart, 'Start'], [windowEnd, 'Finish']]) {
    if (!when) continue
    shapes.push({
      type: 'line', xref: 'x', yref: 'paper',
      x0: londonTime(when), x1: londonTime(when), y0: 0, y1: 1,
      line: { color: '#2457c5', width: 2 },
    })
    annotations.push({
      x: londonTime(when), y: 1, xref: 'x', yref: 'paper', yanchor: 'bottom',
      text: label, showarrow: false, font: { size: 11, color: '#2457c5' },
    })
  }
  shapes.push(...outlines) // Last, so the recommended window's outline is always complete

  return (
    <section className="chart-card">
      <Plot
        data={traces}
        layout={{
          title: { text: title, x: 0, font: { size: 16 } },
          autosize: true,
          margin: { l: 48, r: 16, t: 40, b: 40 },
          bargap: 0.15,
          legend: { orientation: 'h', y: -0.25 },
          xaxis: { tickformat: '%a %H:%M', gridcolor: '#e3e3e6' },
          yaxis: { title: { text: unit }, rangemode: 'tozero', gridcolor: '#e3e3e6' },
          paper_bgcolor: 'rgba(0,0,0,0)',
          plot_bgcolor: 'rgba(0,0,0,0)',
          font: { family: 'system-ui, -apple-system, Segoe UI, Roboto, sans-serif', color: '#1d1d1f' },
          shapes,
          annotations,
        }}
        config={{ displayModeBar: false, responsive: true }}
        style={{ width: '100%', height: '300px' }}
        useResizeHandler
      />
      {metric === 'price' && (
        <p className="hint">
          {confirmedUntil
            ? `Confirmed prices until ${new Date(confirmedUntil).toLocaleTimeString('en-GB', {
                weekday: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'Europe/London',
              })}. After that, the chart shows our forecast.`
            : 'All prices shown are our forecast.'}
        </p>
      )}
      <p className="hint">
        Green: the lowest third of the next 48 hours. Red: the highest third.
        The grey band targets 80% coverage. Actual coverage varies and is not guaranteed.
      </p>
    </section>
  )
}
