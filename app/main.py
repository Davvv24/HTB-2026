from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import config
from src import explain, models
from src.optimiser import (
    InfeasibleError, get_appliance, plan_appliance, plan_ev, validate_forecast,
)

FORECASTS_PATH = Path("results/step8/forecasts.csv")
MODEL_DIR = Path("results/step8/models")
FEATURES_PATH = config.DATA_DIR / "features.parquet"
DEFAULT_EV_KWH = 30
VALUE_COLUMNS = ["price_p10", "price_p50", "price_p90",
                 "carbon_p10", "carbon_p50", "carbon_p90"]


def iso(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_latest_forecast():
    """Latest saved model forecast from Step 8: a historical replay, not live.
    Selected the same way as src/explain.py, so the explanations line up."""
    forecasts = pd.read_csv(FORECASTS_PATH)
    forecasts["forecast_at_utc"] = pd.to_datetime(forecasts.forecast_at_utc, utc=True)
    forecasts["target_at_utc"] = pd.to_datetime(forecasts.target_at_utc, utc=True)
    issued_at = forecasts.forecast_at_utc.max()
    table = (forecasts.loc[forecasts.forecast_at_utc == issued_at]
             .set_index("target_at_utc").sort_index())
    table["price_published"] = False
    validate_forecast(table)
    return issued_at, table


def load_contributions(issued_at):
    """The model's feature contributions for that forecast, used for the 'why'."""
    features = models._read_table(FEATURES_PATH)
    features = features.loc[pd.to_datetime(features.forecast_at_utc, utc=True) == issued_at]
    return explain.model_explanations(features, MODEL_DIR)


# Load once when the server starts, not on every request
ISSUED_AT, FORECAST = load_latest_forecast()
try:
    CONTRIBUTIONS = load_contributions(ISSUED_AT)
except Exception as error:
    print(f"Explanations unavailable, using the simple sentence instead: {error}")
    CONTRIBUTIONS = None

app = FastAPI()


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
    slots = [
        {
            "time": iso(t),
            **{col: float(row[col]) for col in VALUE_COLUMNS},
            "price_published": bool(row["price_published"]),
        }
        for t, row in FORECAST.iterrows()
    ]
    return {"data_as_of": iso(ISSUED_AT), "cached": True,
            "historical_demo": True, "slots": slots}


class RecommendRequest(BaseModel):
    appliance: str
    earliest_start: str
    deadline: str
    carbon_weight: float = 0.5   # 0 = cheapest, 1 = greenest
    risk_aversion: float = 0.5
    ev_kwh_needed: float | None = None


@app.post("/api/recommend")
def recommend(req: RecommendRequest):
    options = dict(carbon_weight=req.carbon_weight, risk_aversion=req.risk_aversion)

    try:
        if req.appliance == "ev":
            plan = plan_ev(FORECAST, req.ev_kwh_needed or DEFAULT_EV_KWH,
                           req.earliest_start, req.deadline, **options)
        elif req.appliance in config.APPLIANCES:
            plan = plan_appliance(FORECAST, get_appliance(req.appliance),
                                  req.earliest_start, req.deadline, **options)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown appliance: {req.appliance}")
    except InfeasibleError as error:
        raise HTTPException(status_code=422, detail=str(error))

    result = plan.to_dict()
    try:
        if CONTRIBUTIONS is None:
            raise ValueError("No model contributions loaded")
        result.update(explain.explain_recommendation(plan, FORECAST, CONTRIBUTIONS))
    except (ValueError, KeyError):
        result["why"] = explain.explain_plan(plan)  # Simpler sentence as a fallback

    result.update(data_as_of=iso(ISSUED_AT), cached=True, historical_demo=True)
    return result