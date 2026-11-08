import { lazy, Suspense, useEffect, useState } from 'react'
import './App.css'
import {
  choicesByDay, defaultFinish, planWindow, requestJson, savingText, timeChoices,
} from './request'
import GridHeartbeat from './components/GridHeartbeat'
import WhyPanel from './components/WhyPanel'

const DAY_LABELS = { today: 'Today', tomorrow: 'Tomorrow' }

// Charts pull in Plotly (about 1.5 MB gzipped), so they load after the page appears.
const ForecastChart = lazy(() => import('./components/ForecastChart'))
const Receipts = lazy(() => import('./components/Receipts'))
const Savings = lazy(() => import('./components/Savings'))

const REFRESH_MS = 5 * 60 * 1000 // Re-check live data every 5 minutes

const RISK_OPTIONS = [
  {
    label: 'Best on average',
    value: 0.2,
    hint: 'Picks the best slot on average, even if the forecast for it is less certain.',
  },
  {
    label: 'Balanced',
    value: 0.5,
    hint: 'A good result with a reasonable level of certainty.',
  },
  {
    label: 'Safe bet',
    value: 0.8,
    hint: 'A slightly smaller benefit you can be more confident in.',
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
  // Day is 'today' or 'tomorrow'; hour is a UTC ISO time ('' = now / the default)
  const [startDay, setStartDay] = useState('today')
  const [startHour, setStartHour] = useState('')
  const [finishDay, setFinishDay] = useState('')
  const [finishHour, setFinishHour] = useState('')
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

  // Live data plans from the real time; the historical replay from its issue time
  const planNow = () => (forecast?.historical_demo ? new Date(forecast.data_as_of) : new Date())

  // Hourly start and finish choices, as far as the forecast reaches
  const now = planNow()
  const forecastEnd = forecast?.slots.length
    ? new Date(new Date(forecast.slots.at(-1).time).getTime() + 30 * 60 * 1000)
    : new Date(now.getTime() + 48 * 3600 * 1000)
  // Start: Today offers "Now" plus the hours left; Tomorrow only if the forecast reaches it.
  // A choice that has passed or no longer fits falls back to the first one that does.
  const startByDay = choicesByDay(timeChoices(now, new Date(forecastEnd.getTime() - 3600 * 1000)), now)
  const startDays = startByDay.tomorrow.length ? ['today', 'tomorrow'] : ['today']
  const startDayValue = startDays.includes(startDay) ? startDay : 'today'
  const startOptions = startDayValue === 'today'
    ? [{ value: '', label: 'Now' }, ...startByDay.today]
    : startByDay.tomorrow
  const startValue = startOptions.some((o) => o.value === startHour) ? startHour : startOptions[0].value
  const startAt = startValue ? new Date(startValue) : now

  // Finish: only hours after the start and within the forecast; days with none are hidden.
  // Default: the next 07:00, else the latest hour on the chosen day.
  const finishByDay = choicesByDay(timeChoices(startAt, forecastEnd, now), now)
  const finishDays = ['today', 'tomorrow'].filter((day) => finishByDay[day].length)
  const usual = defaultFinish([...finishByDay.today, ...finishByDay.tomorrow], startAt)
  const usualDay = finishByDay.today.some((o) => o.value === usual) ? 'today' : 'tomorrow'
  const finishDayValue = finishDays.includes(finishDay) ? finishDay : usualDay
  const finishOptions = finishByDay[finishDayValue] ?? []
  const finishValue = finishOptions.some((o) => o.value === finishHour) ? finishHour
    : finishOptions.some((o) => o.value === usual) ? usual
      : finishOptions.at(-1)?.value ?? ''

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
      const { start, deadline } = planWindow(planNow(), startValue, finishValue)
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

      <GridHeartbeat grid={grid} />

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
              <span className="day-time">
                <select value={startDayValue} aria-label="Start day"
                  onChange={(e) => { setStartDay(e.target.value); setStartHour('') }}>
                  {startDays.map((day) => <option key={day} value={day}>{DAY_LABELS[day]}</option>)}
                </select>
                <select value={startValue} aria-label="Start time"
                  onChange={(e) => setStartHour(e.target.value)}>
                  {startOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              </span>
            </label>

            <label className="field">
              Finish by
              {finishDays.length === 0 ? (
                <select disabled aria-label="Finish time">
                  <option>No time left in the forecast</option>
                </select>
              ) : (
                <span className="day-time">
                  <select value={finishDayValue} aria-label="Finish day"
                    onChange={(e) => { setFinishDay(e.target.value); setFinishHour('') }}>
                    {finishDays.map((day) => <option key={day} value={day}>{DAY_LABELS[day]}</option>)}
                  </select>
                  <select value={finishValue} aria-label="Finish time"
                    onChange={(e) => setFinishHour(e.target.value)}>
                    {finishOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                </span>
              )}
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
                windowStart={startAt.toISOString()}
                windowEnd={finishValue || null}
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