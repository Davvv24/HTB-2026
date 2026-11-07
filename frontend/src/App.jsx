import { useEffect, useState } from 'react'
import './App.css'

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

// "07:00" -> the next time it's 07:00, as a UTC timestamp for the API
function nextOccurrence(hhmm) {
  const [h, m] = hhmm.split(':').map(Number)
  const d = new Date()
  d.setHours(h, m, 0, 0)
  if (d <= new Date()) d.setDate(d.getDate() + 1)
  return d.toISOString()
}

// UTC timestamp from the API -> "02:30" in UK time
function ukTime(iso) {
  return new Date(iso).toLocaleTimeString('en-GB', {
    hour: '2-digit', minute: '2-digit', timeZone: 'Europe/London',
  })
}

function App() {
  const [appliance, setAppliance] = useState('washing_machine')
  const [finishBy, setFinishBy] = useState('07:00')
  const [carbonWeight, setCarbonWeight] = useState(50)
  const [riskAversion, setRiskAversion] = useState(0.5)
  const [grid, setGrid] = useState(null)
  const [rec, setRec] = useState(null)

  const selectedRisk = RISK_OPTIONS.find((o) => o.value === riskAversion)

  useEffect(() => {
    fetch('/api/grid')
      .then((res) => res.json())
      .then((data) => setGrid(data))
  }, [])

  async function getRecommendation() {
    const res = await fetch('/api/recommend', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        appliance,
        earliest_start: new Date().toISOString(),
        deadline: nextOccurrence(finishBy),
        carbon_weight: carbonWeight / 100,
        risk_aversion: riskAversion,
      }),
    })
    setRec(await res.json())
  }

  return (
    <main className="app">
      <h1>When should I run it?</h1>

      <p className="grid-status">
        {grid
          ? `Grid carbon intensity now: ${grid.carbon_intensity} gCO₂/kWh`
          : 'Loading grid data…'}
      </p>

      <section className="controls">
        <div className="row">
          <label className="field">
            Appliance
            <select value={appliance} onChange={(e) => setAppliance(e.target.value)}>
              <option value="washing_machine">Washing machine</option>
              <option value="dishwasher">Dishwasher</option>
              <option value="tumble_dryer">Tumble dryer</option>
              <option value="ev">Electric car</option>
              <option value="battery">Home battery</option>
            </select>
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

        <button className="primary" onClick={getRecommendation}>
          Find the best time
        </button>
      </section>

      {rec && (
        <section className="rec-card">
          <h2>Run it at {ukTime(rec.start)}</h2>
          <p>
            Saves £{rec.saving_gbp.toFixed(2)} and {rec.saving_co2_kg} kg CO₂
            compared with running now.
          </p>
          <p className="why">Why: {rec.why}</p>
        </section>
      )}
    </main>
  )
}

export default App