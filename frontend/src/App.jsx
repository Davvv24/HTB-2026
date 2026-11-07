import { useEffect, useState } from 'react'
import './App.css'

function App() {
  const [appliance, setAppliance] = useState('washing_machine')
  const [finishBy, setFinishBy] = useState('07:00')
  const [grid, setGrid] = useState(null)

  // Runs once when the page loads: ask FastAPI for the grid data
  useEffect(() => {
    fetch('/api/grid')
      .then((res) => res.json())
      .then((data) => setGrid(data))
  }, [])

  return (
    <main>
      <h1>When should I run it?</h1>

      <p>
        {grid
          ? `Grid carbon intensity now: ${grid.carbon_intensity} gCO₂/kWh`
          : 'Loading grid data…'}
      </p>

      <label>
        Appliance:
        <select value={appliance} onChange={(e) => setAppliance(e.target.value)}>
          <option value="washing_machine">Washing machine</option>
          <option value="dishwasher">Dishwasher</option>
          <option value="tumble_dryer">Tumble dryer</option>
          <option value="ev">Electric car</option>
          <option value="battery">Home battery</option>
        </select>
      </label>

      <label>
        Finish by:
        <input type="time" value={finishBy} onChange={(e) => setFinishBy(e.target.value)} />
      </label>

      <p>You chose {appliance}, finishing by {finishBy}.</p>
    </main>
  )
}

export default App