from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/grid")
def grid():
    # Mock data for now; later this calls the team's code in /src
    return {
        "data_as_of": "2026-11-07T18:00:00Z",
        "cached": True,
        "carbon_intensity": 200,
        "mix_percent": {
            "wind": 42, "gas": 25, "nuclear": 15, "solar": 3,
            "imports": 8, "biomass": 5, "other": 2,
        },
    }

class RecommendRequest(BaseModel):
    appliance: str
    earliest_start: str
    deadline: str
    carbon_weight: float = 0.5   # 0 = cheapest, 1 = greenest
    risk_aversion: float = 0.5
    ev_kwh_needed: float | None = None


@app.post("/api/recommend")
def recommend(req: RecommendRequest):
    # Mock for now; later this calls the team's optimiser in /src
    return {
        "data_as_of": "2026-11-07T18:00:00Z",
        "cached": True,
        "appliance": req.appliance,
        "start": "2026-11-08T02:30:00Z",
        "end": "2026-11-08T04:30:00Z",
        "cost_gbp": 0.31,
        "co2_kg": 0.4,
        "run_now": {"start": req.earliest_start, "cost_gbp": 0.73, "co2_kg": 1.5},
        "saving_gbp": 0.42,
        "saving_co2_kg": 1.1,
        "why": "Strong wind in Scotland and low overnight demand",
    }