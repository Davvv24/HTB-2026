import math

import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import config
from src.optimiser import InfeasibleError, get_appliance, plan_appliance, plan_ev

app = FastAPI()

DEFAULT_EV_KWH = 30  # Used if the user doesn't say how much charge the car needs
VALUE_COLUMNS = ["price_p10", "price_p50", "price_p90",
                 "carbon_p10", "carbon_p50", "carbon_p90"]


def iso(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def get_forecast_table():
    """48h forecast in the format optimiser.py expects.
    Mock for now; later replaced by the Step 5-6 model output."""
    now = pd.Timestamp.now(tz="UTC")
    index = pd.date_range(now.floor("30min"), periods=96, freq="30min", name="time_utc")

    rows = []
    for i, t in enumerate(index):
        hour = t.hour + t.minute / 60
        carbon = 150 + 60 * math.sin((hour - 11) / 24 * 2 * math.pi)
        price = 18 + 6 * math.sin((hour - 10) / 24 * 2 * math.pi)
        if 16 <= hour < 19:
            price += 12

        published = i < 12
        spread = 0.08 + 0.004 * i
        price_spread = 0 if published else spread  # Published prices are known exactly

        rows.append({
            "price_p10": round(price * (1 - price_spread), 2),
            "price_p50": round(price, 2),
            "price_p90": round(price * (1 + price_spread), 2),
            "carbon_p10": round(carbon * (1 - spread)),
            "carbon_p50": round(carbon),
            "carbon_p90": round(carbon * (1 + spread)),
            "price_published": published,
        })
    return pd.DataFrame(rows, index=index)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/grid")
def grid():
    # Mock for now; later uses the team's data fetching in /src
    return {
        "data_as_of": "2026-11-07T18:00:00Z",
        "cached": True,
        "carbon_intensity": 145,
        "mix_percent": {
            "wind": 42, "gas": 25, "nuclear": 15, "solar": 3,
            "imports": 8, "biomass": 5, "other": 2,
        },
    }


@app.get("/api/forecast")
def forecast():
    table = get_forecast_table()
    slots = [
        {
            "time": iso(t),
            **{col: float(row[col]) for col in VALUE_COLUMNS},
            "price_published": bool(row["price_published"]),
        }
        for t, row in table.iterrows()
    ]
    return {"data_as_of": iso(table.index[0]), "cached": True, "slots": slots}


class RecommendRequest(BaseModel):
    appliance: str
    earliest_start: str
    deadline: str
    carbon_weight: float = 0.5   # 0 = cheapest, 1 = greenest
    risk_aversion: float = 0.5
    ev_kwh_needed: float | None = None


@app.post("/api/recommend")
def recommend(req: RecommendRequest):
    table = get_forecast_table()
    options = dict(carbon_weight=req.carbon_weight, risk_aversion=req.risk_aversion)

    try:
        if req.appliance == "ev":
            plan = plan_ev(table, req.ev_kwh_needed or DEFAULT_EV_KWH,
                           req.earliest_start, req.deadline, **options)
        elif req.appliance in config.APPLIANCES:
            plan = plan_appliance(table, get_appliance(req.appliance),
                                  req.earliest_start, req.deadline, **options)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown appliance: {req.appliance}")
    except InfeasibleError as error:
        # e.g. the deadline is too soon for the appliance to finish
        raise HTTPException(status_code=422, detail=str(error))

    result = plan.to_dict()
    result["data_as_of"] = iso(table.index[0])
    result["cached"] = True
    return result