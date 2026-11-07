import { useEffect, useState } from 'react'
import createPlotlyComponent from 'react-plotly.js/factory'
import Plotly from 'plotly.js-dist-min'
import { requestJson } from '../request'

const Plot = createPlotlyComponent(Plotly)

// Which strategies get a line on the chart, and how they look
const LINE_STYLES = {
  model: { color: '#1a7f4b', width: 3 },
  fixed_overnight: { color: '#b7791f', width: 2 },
  perfect_foresight: { color: '#6b6b70', width: 2, dash: 'dash' },
}

function co2Text(kg) {
  if (Math.abs(kg) < 0.005) return 'no change'
  return kg > 0 ? `${kg.toFixed(2)} kg saved` : `${Math.abs(kg).toFixed(2)} kg extra`
}

export default function Receipts() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    requestJson('/api/backtest')
      .then((result) => setData(result))
      .catch((err) => setError(err.message))
  }, [])

  if (error) return <p className="placeholder">{error}</p>
  if (!data) return <p className="placeholder">Loading results…</p>

  const model = data.strategies.find((s) => s.key === 'model')
  const perfect = data.strategies.find((s) => s.key === 'perfect_foresight')
  const dates = data.cumulative.map((d) => d.date)

  const traces = data.strategies
    .filter((s) => LINE_STYLES[s.key])
    .map((s) => ({
      x: dates,
      y: data.cumulative.map((d) => d[s.key]),
      type: 'scatter',
      mode: 'lines',
      name: s.label,
      line: LINE_STYLES[s.key],
      hovertemplate: `${s.label}: £%{y:.2f}<extra></extra>`,
    }))

  return (
    <div className="receipts">
      <section className="rec-card">
        <h2>{Math.round(data.pct_of_perfect_foresight)}% of the best possible saving</h2>
        <p>
          Over {data.days} days, our model saved £{model.saving_gbp.toFixed(2)} compared with
          starting straight away. Even with perfect knowledge of future prices, the most
          anyone could have saved was £{perfect.saving_gbp.toFixed(2)}.
        </p>
        <p className="for-line">
          Simulated household running a washing machine, dishwasher and tumble dryer once a
          day, tested on days the model never saw during training.
        </p>
      </section>

      <section className="chart-card">
        <Plot
          data={traces}
          layout={{
            title: { text: 'Running total saved vs starting straight away', x: 0, font: { size: 16 } },
            autosize: true,
            margin: { l: 48, r: 16, t: 40, b: 40 },
            legend: { orientation: 'h', y: -0.2 },
            xaxis: { gridcolor: '#e3e3e6' },
            yaxis: { title: { text: '£ saved' }, gridcolor: '#e3e3e6', rangemode: 'tozero' },
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            font: { family: 'system-ui, -apple-system, Segoe UI, Roboto, sans-serif', color: '#1d1d1f' },
          }}
          config={{ displayModeBar: false, responsive: true }}
          style={{ width: '100%', height: '320px' }}
          useResizeHandler
        />
      </section>

      <section className="chart-card">
        <table className="results-table">
          <thead>
            <tr>
              <th>Strategy</th>
              <th>Total cost</th>
              <th>Saved</th>
              <th>CO₂</th>
              <th>Share of best saving</th>
            </tr>
          </thead>
          <tbody>
            {data.strategies.map((s) => (
              <tr key={s.key} className={s.key === 'model' ? 'highlight' : undefined}>
                <td>{s.label}</td>
                <td>£{s.cost_gbp.toFixed(2)}</td>
                <td>£{s.saving_gbp.toFixed(2)}</td>
                <td>{co2Text(s.saving_kg)}</td>
                <td>{Math.round(s.pct_captured)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data.carbon_weight === 0 && (
          <p className="hint">
            This test optimised for cost only, so carbon savings are small. The cheapest time
            isn't always the greenest, which is why the app lets you choose.
          </p>
        )}
      </section>

      <section className="chart-card">
        <h3>Home battery</h3>
        <p>
          A separate simulated home with a battery saved £{data.battery.net_saving_gbp.toFixed(2)} over
          the same {data.days} days, after allowing for battery wear.
        </p>
      </section>

      <details className="chart-card">
        <summary>How we tested this</summary>
        <ul>
          {data.assumptions.map((a) => <li key={a}>{a}</li>)}
        </ul>
      </details>
    </div>
  )
}