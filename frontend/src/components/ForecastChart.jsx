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

export default function ForecastChart({ slots, metric, unit, title, rec }) {
  const times = slots.map((s) => londonTime(s.time))
  const p10 = slots.map((s) => s[`${metric}_p10`])
  const p50 = slots.map((s) => s[`${metric}_p50`])
  const p90 = slots.map((s) => s[`${metric}_p90`])

  const traces = [
    {
      x: times, y: p50, type: 'bar', name: 'Most likely',
      marker: { color: levelColours(p50) },
      hovertemplate: `%{x|%a %H:%M}: %{y} ${unit}<extra></extra>`,
    },
    // Invisible top edge of the band...
    { x: times, y: p90, type: 'scatter', mode: 'lines', line: { width: 0 },
      hoverinfo: 'skip', showlegend: false },
    // ...and the bottom edge, filled up to the top edge = the 80% range
    { x: times, y: p10, type: 'scatter', mode: 'lines', line: { width: 0 },
      fill: 'tonexty', fillcolor: 'rgba(29, 29, 31, 0.15)',
      name: '80% likely range', hoverinfo: 'skip' },
  ]

  // Shade the recommended window, once there is a recommendation
  const shapes = rec
    ? [{
        type: 'rect', xref: 'x', yref: 'paper',
        x0: londonTime(rec.start), x1: londonTime(rec.end), y0: 0, y1: 1,
        fillcolor: 'rgba(26, 127, 75, 0.12)',
        line: { color: '#1a7f4b', width: 1 },
        layer: 'below',
      }]
    : []

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
        }}
        config={{ displayModeBar: false, responsive: true }}
        style={{ width: '100%', height: '300px' }}
        useResizeHandler
      />
      <p className="hint">
        Green: the lowest third of the next 48 hours. Red: the highest third.
        The grey band is where the true value will fall 80% of the time.
      </p>
    </section>
  )
}