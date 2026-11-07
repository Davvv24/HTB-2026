import { lazy, Suspense, useEffect, useState } from 'react'
import './App.css'
import { nextOccurrence, requestJson, savingText } from './request'
import WhyPanel from './components/WhyPanel'

// Charts pull in Plotly (about 1.5 MB gzipped), so they load after the page appears.
const ForecastChart = lazy(() => import('./components/ForecastChart'))
const Receipts = lazy(() => import('./components/Receipts'))
const Savings = lazy(() => import('./components/Savings'))

const REFRESH_MS = 5 * 60 * 1000 // Re-check live data every 5 minutes

const RISK_OPTIONS = [
  {
    label: 'Max saving',
    value: 0.2,
    hint: 'Aims for the biggest saving, even if the price is less certain.',
  },
  {
    label: 'Balanced',
    value: 0.5,
    hint: 'A good saving with a reasonable level of certainty.',
  },
  {
    label: 'Safe bet',
    value: 0.8,
    hint: 'A slightly smaller saving you can be more confident in.',
  },
]

const APPLIANCE_LABELS = {
  washing_machine: 'Washing machine',
  dishwasher: 'Dishwasher',
  tumble_dryer: 'Tumble dryer',
  ev: 'Electric car',
}

const METRICS = {
  price: { label: 'Price', unit: 'p/kWh', title: 'Price, next 48 hours' },
  carbon: { label: 'Carbon', unit: 'gCO₂/kWh', title: 'Carbon intensity, next 48 hours' },
}

// UTC timestamp -> "Mon 02:30" in UK time
function ukTime(iso) {
  return new Date(iso).toLocaleTimeString('en-GB', {
    weekday: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'Europe/London',
  })
}

// One line saying where the forecast comes from and how fresh it is
function forecastStatus(forecast) {
  if (forecast.historical_demo) {
    return `Demo: replaying our model's forecast issued ${ukTime(forecast.data_as_of)}`
  }
  const checked = forecast.last_updated ? ` Prices checked ${ukTime(forecast.last_updated)}.` : ''
  if (forecast.source === 'model') {
    return `Live: our model's forecast from ${ukTime(forecast.data_as_of)}, with Octopus's confirmed prices where published.${checked}`
  }
  return `Live: Octopus's confirmed prices and National Grid's carbon forecast (our model forecast isn't ready yet).${checked}`
}

function App() {
  const [view, setView] = useState('plan')
  const [appliance, setAppliance] = useState('washing_machine')
  const [startAfter, setStartAfter] = useState('')
  const [finishBy, setFinishBy] = useState('07:00')
  const [carbonWeight, setCarbonWeight] = useState(50)
  const [riskAversion, setRiskAversion] = useState(0.5)
  const [grid, setGrid] = useState(null)
  const [rec, setRec] = useState(null)
  const [recFor, setRecFor] = useState(null)
  const [requestCount, setRequestCount] = useState(0)
  const [loading, setLoading] = useState(false)
  const [forecast, setForecast] = useState(null)
  const [metric, setMetric] = useState('price')
  const [error, setError] = useState(null)
  const [evKwh, setEvKwh] = useState(30)

  const selectedRisk = RISK_OPTIONS.find((o) => o.value === riskAversion)
  const chart = METRICS[metric]

  // End of the confirmed (published) prices, if any
  const lastPublished = forecast?.slots.filter((s) => s.price_published).at(-1)
  const confirmedUntil = lastPublished
    ? new Date(new Date(lastPublished.time).getTime() + 30 * 60 * 1000).toISOString()
    : null

  useEffect(() => {
    function load() {
      requestJson('/api/grid')
        .then((data) => setGrid(data))
        .catch((error) => setError(error.message))
      requestJson('/api/forecast')
        .then((data) => setForecast(data))
        .catch((error) => setError(error.message))
    }
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => clearInterval(timer)
  }, [])

  async function getRecommendation() {
    if (!forecast) return
    setLoading(true)
    try {
      // Live data plans from the real time; the historical replay from its issue time
      const now = forecast.historical_demo ? new Date(forecast.data_as_of) : new Date()
      const start = startAfter ? nextOccurrence(startAfter, now) : now
      const deadline = nextOccurrence(finishBy, start)
      const data = await requestJson('/api/recommend', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          appliance,
          earliest_start: start.toISOString(),
          deadline: deadline.toISOString(),
          carbon_weight: carbonWeight / 100,
          risk_aversion: riskAversion,
          ev_kwh_needed: appliance === 'ev' ? Number(evKwh) : null,
        }),
      })
      setError(null)
      setRec(data)
      setRecFor({ appliance, deadline: deadline.toISOString(), risk: selectedRisk.label })
      setRequestCount((n) => n + 1)
    } catch (error) {
      setError(error.message)
      setRec(null)
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="app">
      <h1>When should I run it?</h1>

      <p className="grid-status">
        {grid
          ? `GB grid carbon intensity at ${ukTime(grid.data_as_of)}: ${grid.carbon_intensity} gCO₂/kWh`
          : 'Loading grid data…'}
        {grid?.cached && <span className="badge">Saved reading</span>}
      </p>

      {forecast && (
        <p className="grid-status">
          {forecastStatus(forecast)}
          {forecast.cached && !forecast.historical_demo && <span className="badge">Cached data</span>}
        </p>
      )}
      <div className="segmented small tabs">
        <button
          type="button"
          aria-pressed={view === 'plan'}
          onClick={() => setView('plan')}
        >
          Plan
        </button>
        <button
          type="button"
          aria-pressed={view === 'receipts'}
          onClick={() => setView('receipts')}
        >
          Receipts
        </button>
        <button
          type="button"
          aria-pressed={view === 'savings'}
          onClick={() => setView('savings')}
        >
          My Savings
        </button>
      </div>

      <div className="layout" hidden={view !== 'plan'}>
        <section className="controls">
          <label className="field">
            Appliance
            <select value={appliance} onChange={(e) => setAppliance(e.target.value)}>
              <option value="washing_machine">Washing machine</option>
              <option value="dishwasher">Dishwasher</option>
              <option value="tumble_dryer">Tumble dryer</option>
              <option value="ev">Electric car</option>
            </select>
          </label>

          {appliance === 'ev' && (
            <label className="field">
              Energy needed (kWh)
              <input type="number" min="0.1" step="0.1" value={evKwh}
                onChange={(event) => setEvKwh(event.target.value)} />
            </label>
          )}

          <div className="row">
            <label className="field">
              Start after
              <input type="time" value={startAfter} onChange={(e) => setStartAfter(e.target.value)} />
            </label>

            <label className="field">
              Finish by
              <input type="time" value={finishBy} onChange={(e) => setFinishBy(e.target.value)} />
            </label>
          </div>

          <label className="field">
            What matters more?
            <input
              type="range" min="0" max="100"
              value={carbonWeight}
              onChange={(e) => setCarbonWeight(Number(e.target.value))}
            />
            <span className="slider-ends">
              <span>Cheapest</span>
              <span>Greenest</span>
            </span>
          </label>

          <fieldset className="field">
            <legend>How sure do you want to be?</legend>
            <div className="segmented">
              {RISK_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  aria-pressed={riskAversion === option.value}
                  onClick={() => setRiskAversion(option.value)}
                >
                  {option.label}
                </button>
              ))}
            </div>
            <p className="hint">{selectedRisk.hint}</p>
          </fieldset>

          <button className="primary" onClick={getRecommendation} disabled={loading || !forecast}>
            {loading ? 'Finding the best time…' : 'Find the best time'}
          </button>
        </section>

        <div className="results">
          {error && <p className="placeholder">{error}</p>}

          {rec ? (
            <section className="rec-card" key={requestCount}>
              <h2>Run it at {ukTime(rec.start)}</h2>
              <p>
                Estimated cost is {savingText(rec.saving_gbp, 'GBP')} and emissions are{' '}
                {savingText(rec.saving_co2_kg, 'kg CO₂', 3)} compared with starting at{' '}
                {ukTime(rec.run_now.start)}.
              </p>
              <p className="for-line">
                For: {APPLIANCE_LABELS[recFor.appliance]}, finishing by {ukTime(recFor.deadline)},{' '}
                {recFor.risk.toLowerCase()}.
              </p>
              <p className="for-line">
                {!rec.uses_forecast
                  ? 'Based on confirmed prices from Octopus.'
                  : confirmedUntil
                    ? `Prices after ${ukTime(confirmedUntil)} aren't out yet, so this plan uses our forecast for them.`
                    : 'This plan uses our price forecast.'}
              </p>
              <div className="compare">
                <div>
                  <p className="compare-label">Start straight away ({ukTime(rec.run_now.start)})</p>
                  <p className="compare-value">£{rec.run_now.cost_gbp.toFixed(2)}</p>
                  <p className="compare-sub">{rec.run_now.co2_kg.toFixed(2)} kg CO₂</p>
                </div>
                <div className="compare-best">
                  <p className="compare-label">Wait until {ukTime(rec.start)}</p>
                  <p className="compare-value">£{rec.cost_gbp.toFixed(2)}</p>
                  <p className="compare-sub">{rec.co2_kg.toFixed(2)} kg CO₂</p>
                </div>
              </div>
              <details className="why">
                <summary>Why this time?</summary>
                <WhyPanel drivers={rec.model_drivers} />
                {rec.interpretation && <p className="hint">{rec.interpretation}</p>}
              </details>
            </section>
          ) : (
            !error && (
              <p className="placeholder">
                Choose your appliance and when it needs to finish, then press
                “Find the best time”.
              </p>
            )
          )}

          <div className="results-header">
            <h2>Next 48 hours</h2>
            <div className="segmented small">
              {Object.entries(METRICS).map(([key, m]) => (
                <button
                  key={key}
                  type="button"
                  aria-pressed={metric === key}
                  onClick={() => setMetric(key)}
                >
                  {m.label}
                </button>
              ))}
            </div>
          </div>

          {forecast ? (
            <Suspense fallback={<p className="placeholder">Loading chart…</p>}>
              <ForecastChart
                slots={forecast.slots}
                metric={metric}
                unit={chart.unit}
                title={chart.title}
                rec={rec}
                confirmedUntil={confirmedUntil}
              />
            </Suspense>
          ) : (
            <p className="placeholder">Loading forecast…</p>
          )}
        </div>
      </div>
      <Suspense fallback={<p className="placeholder">Loading…</p>}>
        {view === 'receipts' && <Receipts />}
        {view === 'savings' && <Savings />}
      </Suspense>
    </main>
  )
}

export default App
