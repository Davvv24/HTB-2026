from fastapi import FastAPI
from pydantic import BaseModel
import math
from datetime import datetime, timedelta, timezone

app = FastAPI()

@app.get("/api/forecast")
def forecast():
    # Mock 48h forecast with a realistic daily shape.
    # Later replaced by the team's model output from /src.
    now = datetime.now(timezone.utc)
    start = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)

    slots = []
    for i in range(96):  # 96 half-hours = 48h
        t = start + timedelta(minutes=30 * i)
        hour = t.hour + t.minute / 60

        # Carbon: peaks around 17:00, lowest around 05:00
        carbon = 150 + 60 * math.sin((hour - 11) / 24 * 2 * math.pi)
        # Price: daily cycle plus an evening peak 16:00-19:00
        price = 18 + 6 * math.sin((hour - 10) / 24 * 2 * math.pi)
        if 16 <= hour < 19:
            price += 12

        spread = 0.08 + 0.004 * i  # uncertainty grows further ahead

        slots.append({
            "time": t.isoformat().replace("+00:00", "Z"),
            "price_p10": round(price * (1 - spread), 2),
            "price_p50": round(price, 2),
            "price_p90": round(price * (1 + spread), 2),
            "price_published": i < 12,
            "carbon_p10": round(carbon * (1 - spread)),
            "carbon_p50": round(carbon),
            "carbon_p90": round(carbon * (1 + spread)),
            "official_carbon": round(carbon * 1.05),
        })

    return {
        "data_as_of": now.isoformat().replace("+00:00", "Z"),
        "cached": True,
        "slots": slots,
    }

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