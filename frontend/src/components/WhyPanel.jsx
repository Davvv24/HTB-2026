// Turns explain.py's model_drivers into a short plain-English list
const SOURCES = [
  { key: 'price', title: 'Cost', min: 0.005, format: (v) => `£${Math.abs(v).toFixed(2)}` },
  { key: 'carbon', title: 'Carbon', min: 0.005, format: (v) => `${Math.round(Math.abs(v) * 1000)} g CO₂` },
]

// Technical model terms that don't mean anything to a household user
const HIDDEN = ['Model reference value', 'Non-negative carbon adjustment', 'Calibration adjustment']

export default function WhyPanel({ drivers, why }) {
  if (!drivers) return why ? <p>{why}</p> : null

  return (
    <div className="why-panel">
      {why && <p>{why}</p>}
      <p className="hint">
        Compared with starting straight away, these parts of our forecast make your slot
        cheaper or greener:
      </p>

      {SOURCES.map(({ key, title, min, format }) => {
        const detail = drivers[key]
        if (!detail) return null

        if (detail.status === 'published_prices') {
          return (
            <div key={key}>
              <h4>{title}</h4>
              <p className="hint">These prices are already published, so there's nothing to forecast.</p>
            </div>
          )
        }

        if (detail.status === 'partly_published') {
          return (
            <div key={key}>
              <h4>{title}</h4>
              <p className="hint">
                Starting straight away uses Octopus's confirmed prices, but this time uses our
                forecast, so the cost difference can't be split into forecast factors.
              </p>
            </div>
          )
        }

        const top = (detail.drivers ?? [])
          .filter((d) => !HIDDEN.includes(d.factor) && Math.abs(d.contribution) >= min)
          .slice(0, 3)

        return (
          <div key={key}>
            <h4>{title}</h4>
            {top.length === 0 ? (
              <p className="hint">No single factor stands out.</p>
            ) : (
              <ul>
                {top.map((d) => (
                  <li key={d.factor} className={d.contribution < 0 ? 'helps' : 'hurts'}>
                    <span>{d.factor}</span>
                    <span>{d.contribution < 0 ? 'lowers it by ' : 'raises it by '}{format(d.contribution)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )
      })}
    </div>
  )
}
