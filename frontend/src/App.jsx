import { useState } from 'react'
import './App.css'

function App() {
  const [appliance, setAppliance] = useState('washing')
  const [finishBy, setFinishBy] = useState('07:00')

  return (
    <main>
      <h1>When should I run it?</h1>

      <label>
        Appliance:
        <select value={appliance} onChange={(e) => setAppliance(e.target.value)}>
          <option value="washing">Washing machine</option>
          <option value="dishwasher">Dishwasher</option>
          <option value="ev">Electric car</option>
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