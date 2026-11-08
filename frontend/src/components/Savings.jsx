import { useEffect, useState } from 'react'
import createPlotlyComponent from 'react-plotly.js/factory'
import Plotly from 'plotly.js-dist-min'
import { requestJson } from '../request'

const Plot = createPlotlyComponent(Plotly)

function shortDate(iso) {
  return new Date(iso).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })
}

// A plain-English nudge based on progress towards the goal
function nudge(h) {
  if (h.goal_reached) {
    return `Goal reached: £${h.saved_gbp.toFixed(2)} saved this month. Keep your streak going!`
  }
  const remaining = h.monthly_goal_gbp - h.saved_gbp
  const perDay = h.saved_gbp / h.days
  const daysToGo = perDay > 0 ? Math.ceil(remaining / perDay) : null
  return daysToGo
    ? `£${remaining.toFixed(2)} to go. At your recent pace of £${perDay.toFixed(2)} a day, that's about ${daysToGo} more days of following the app.`
    : `£${remaining.toFixed(2)} to go.`
}

export default function Savings() {
  const [households, setHouseholds] = useState(null)
  const [selected, setSelected] = useState('student_flat')
  const [error, setError] = useState(null)

  useEffect(() => {
    requestJson('/api/households')
      .then((data) => setHouseholds(data.households))
      .catch((err) => setError(err.message))
  }, [])

  if (error) return <p className="placeholder">{error}</p>
  if (!households) return <p className="placeholder">Loading savings…</p>

  const h = households.find((x) => x.key === selected) ?? households[0]
  const first = h.daily[0]?.date
  const last = h.daily.at(-1)?.date
  const avoidedPct = h.avoidable_gbp > 0
    ? Math.round(Math.min(h.saved_gbp / h.avoidable_gbp, 1) * 100)
    : null

  return (
    <div className="savings">
      <div className="segmented small tabs">
        {households.map((x) => (
          <button
            key={x.key}
            type="button"
            aria-pressed={x.key === h.key}
            onClick={() => setSelected(x.key)}
          >
            {x.label}
          </button>
        ))}
      </div>

      <section className="rec-card">
        <p className="badge">Simulated household</p>
        <h2>{h.label}</h2>
        <p className="for-line">{h.description}</p>

        <div className="goal">
          <div className="goal-text">
            <span>Monthly goal</span>
            <span>£{h.saved_gbp.toFixed(2)} of £{h.monthly_goal_gbp.toFixed(2)}</span>
          </div>
          <div
            className="progress"
            role="progressbar"
            aria-valuenow={h.goal_progress_pct}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label="Progress towards monthly goal"
          >
            <div className="progress-fill" style={{ width: `${h.goal_progress_pct}%` }} />
          </div>
        </div>

        <p>{nudge(h)}</p>
      </section>

      <section className="stats">
        <div className="stat">
          <p className="stat-value">{h.current_streak_days}</p>
          <p className="stat-label">day streak without a loss</p>
        </div>
        <div className="stat">
          <p className="stat-value">{h.green_points}</p>
          <p className="stat-label">green points</p>
        </div>
        <div className="stat">
          <p className="stat-value">{h.days_saved}/{h.days}</p>
          <p className="stat-label">days that saved money</p>
        </div>
        <div className="stat">
          <p className="stat-value">{h.saved_co2_kg.toFixed(1)} kg</p>
          <p className="stat-label">CO₂ saved</p>
        </div>
      </section>

      {avoidedPct !== null && (
        <section className="rec-card">
          <h3>The invisible cost this month: £{h.avoidable_gbp.toFixed(2)}</h3>
          <p>
            Running everything as soon as you could would have cost £{h.avoidable_gbp.toFixed(2)} more
            than the best possible times. You'd never see it on a bill.
          </p>
          <div
            className="progress"
            role="progressbar"
            aria-valuenow={avoidedPct}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label="Share of the invisible cost avoided"
          >
            <div className="progress-fill" style={{ width: `${avoidedPct}%` }} />
          </div>
          <p>
            <strong>
              By following the app, you avoided £{h.saved_gbp.toFixed(2)} of it ({avoidedPct}%).
            </strong>
          </p>
        </section>
      )}

      <section className="chart-card">
        <Plot
          data={[{
            x: h.daily.map((d) => d.date),
            y: h.daily.map((d) => d.saving_gbp),
            type: 'bar',
            marker: { color: h.daily.map((d) => (d.saving_gbp < 0 ? '#c0392b' : '#1a7f4b')) },
            hovertemplate: '%{x|%a %d %b}: £%{y:.2f}<extra></extra>',
          }]}
          layout={{
            title: { text: 'Saved each day', x: 0, font: { size: 16 } },
            autosize: true,
            margin: { l: 48, r: 16, t: 40, b: 40 },
            xaxis: { gridcolor: '#e3e3e6' },
            yaxis: { title: { text: '£' }, gridcolor: '#e3e3e6' },
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            font: { family: 'system-ui, -apple-system, Segoe UI, Roboto, sans-serif', color: '#1d1d1f' },
          }}
          config={{ displayModeBar: false, responsive: true }}
          style={{ width: '100%', height: '220px' }}
          useResizeHandler
        />
        <p className="hint">
          Simulated: what this household would have saved by following our recommendations
          from {shortDate(first)} to {shortDate(last)}, during our backtest. Points: 10 for each
          day that saved money, plus 1 per 100 g of CO₂ saved.
        </p>
      </section>
    </div>
  )
}