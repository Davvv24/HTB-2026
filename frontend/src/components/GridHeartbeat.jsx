// Colours by source: greens for renewables, blue for nuclear, amber/brown for fossil
const SOURCES = {
  wind: { label: 'Wind', color: '#1a7f4b' },
  solar: { label: 'Solar', color: '#7cb342' },
  hydro: { label: 'Hydro', color: '#4f9d8f' },
  nuclear: { label: 'Nuclear', color: '#6a8caf' },
  biomass: { label: 'Biomass', color: '#a1887f' },
  imports: { label: 'Imports', color: '#bdbdbd' },
  gas: { label: 'Gas', color: '#b7791f' },
  coal: { label: 'Coal', color: '#5d4037' },
  other: { label: 'Other', color: '#d6d6d6' },
}
const LOW_CARBON = ['wind', 'solar', 'hydro', 'nuclear']

const label = (key) => SOURCES[key]?.label ?? key

export default function GridHeartbeat({ grid }) {
  if (!grid) return <p className="grid-status">Loading grid data…</p>

  const parts = Object.entries(grid.mix_percent)
    .filter(([, pct]) => pct > 0)
    .sort((a, b) => b[1] - a[1])
  const [topKey, topPct] = parts[0]
  const lowCarbon = LOW_CARBON.reduce((sum, key) => sum + (grid.mix_percent[key] ?? 0), 0)
  const time = new Date(grid.data_as_of).toLocaleTimeString('en-GB', {
    weekday: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'Europe/London',
  })

  return (
    <section className="heartbeat">
      <div className="heartbeat-head">
        {grid.source === 'live' && <span className="pulse" aria-hidden="true" />}
        <strong>{grid.carbon_intensity} gCO₂/kWh</strong>
        <span className="hint">
          GB grid at {time} · {Math.round(lowCarbon)}% low-carbon · mostly{' '}
          {label(topKey).toLowerCase()} ({Math.round(topPct)}%)
        </span>
      </div>

      <div
        className="mix-bar"
        role="img"
        aria-label={parts.map(([key, pct]) => `${label(key)} ${Math.round(pct)}%`).join(', ')}
      >
        {parts.map(([key, pct]) => (
          <div
            key={key}
            title={`${label(key)}: ${pct}%`}
            style={{ width: `${pct}%`, background: SOURCES[key]?.color ?? '#ccc' }}
          />
        ))}
      </div>

      <ul className="mix-legend">
        {parts.filter(([, pct]) => pct >= 1).map(([key, pct]) => (
          <li key={key}>
            <span className="swatch" style={{ background: SOURCES[key]?.color }} />
            {label(key)} {Math.round(pct)}%
          </li>
        ))}
      </ul>
    </section>
  )
}