import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import AwareDatetime, BaseModel, Field, model_validator

import config
from src import explain, models
from src.optimiser import (
    InfeasibleError, get_appliance, plan_appliance, plan_ev, validate_forecast,
)

FORECASTS_PATH = config.ROOT_DIR / "results/step8/forecasts.csv"
MODEL_DIR = config.ROOT_DIR / "results/step8/models"
FEATURES_PATH = config.DATA_DIR / "features.parquet"
DEFAULT_EV_KWH = 30
VALUE_COLUMNS = ["price_p10", "price_p50", "price_p90",
                 "carbon_p10", "carbon_p50", "carbon_p90"]
DATASET_PATH = config.DATA_DIR / "dataset.parquet"
MIX_SOURCES = ["wind", "gas", "nuclear", "solar", "biomass", "hydro", "imports", "coal", "other"]


def iso(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")

def published_until(issued_at):
    """Agile prices cover 23:00-23:00 UK time and are published around 16:00
    the day before. Returns when the confirmed prices end, as seen at `issued_at`."""
    local = issued_at.tz_convert(config.UK_TIMEZONE)
    cutoff = local.normalize().replace(hour=23)  # 23:00 today, UK time
    if local.hour >= 16:
        cutoff += pd.Timedelta(days=1)           # tomorrow's prices are out too
    return cutoff.tz_convert("UTC")

def load_latest_forecast():
    """Latest saved model forecast from Step 8: a historical replay, not live.
    Selected the same way as src/explain.py, so the explanations line up."""
    forecasts = pd.read_csv(FORECASTS_PATH)
    forecasts["forecast_at_utc"] = pd.to_datetime(forecasts.forecast_at_utc, utc=True)
    forecasts["target_at_utc"] = pd.to_datetime(forecasts.target_at_utc, utc=True)
    issued_at = forecasts.forecast_at_utc.max()
    table = (forecasts.loc[forecasts.forecast_at_utc == issued_at]
             .set_index("target_at_utc").sort_index())
    # Slots whose Agile price was already published at issue time get the real price
    prices = pd.read_parquet(DATASET_PATH, columns=["time_utc", "price_p_kwh_inc_vat"])
    prices["time_utc"] = pd.to_datetime(prices.time_utc, utc=True)
    prices = prices.set_index("time_utc")["price_p_kwh_inc_vat"].dropna()

    known = (table.index < published_until(issued_at)) & table.index.isin(prices.index)
    for column in ("price_p10", "price_p50", "price_p90"):
        table.loc[known, column] = prices.reindex(table.index[known]).to_numpy()
    table["price_published"] = known
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

def load_grid_snapshot(at):
    """Actual GB carbon intensity and generation mix at the forecast's issue time,
    so the grid panel matches the replay."""
    mix_columns = [f"gb_{source}_pct" for source in MIX_SOURCES]
    data = pd.read_parquet(
        DATASET_PATH,
        columns=["time_utc", "gb_carbon_actual_gco2_kwh", "gb_carbon_actual_suspect", *mix_columns],
    )
    data["time_utc"] = pd.to_datetime(data.time_utc, utc=True)
    usable = (
        (data.time_utc <= at)
        & data.gb_carbon_actual_gco2_kwh.notna()
        & ~data.gb_carbon_actual_suspect.fillna(False).astype(bool)
    )
    row = data[usable].sort_values("time_utc").iloc[-1]  # Latest good reading at or before `at`
    return {
        "data_as_of": iso(row.time_utc),
        "cached": True,
        "historical_demo": True,
        "carbon_intensity": int(row.gb_carbon_actual_gco2_kwh),
        "mix_percent": {
            source: round(float(row[f"gb_{source}_pct"]), 1)
            for source in MIX_SOURCES
            if pd.notna(row[f"gb_{source}_pct"])
        },
    }


try:
    GRID = load_grid_snapshot(ISSUED_AT)
except Exception as error:
    print(f"Grid snapshot unavailable: {error}")
    GRID = None

app = FastAPI()


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/grid")
def grid():
    if GRID is None:
        raise HTTPException(status_code=503, detail="Grid data unavailable")
    return GRID


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
    earliest_start: AwareDatetime
    deadline: AwareDatetime
    carbon_weight: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    risk_aversion: float = Field(default=0.5, ge=0, allow_inf_nan=False)
    ev_kwh_needed: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered_window(self):
        if self.deadline <= self.earliest_start:
            raise ValueError("Deadline must be after the earliest start.")
        return self


@app.post("/api/recommend")
def recommend(req: RecommendRequest):
    options = dict(carbon_weight=req.carbon_weight, risk_aversion=req.risk_aversion)

    try:
        if req.appliance == "ev":
            plan = plan_ev(FORECAST, DEFAULT_EV_KWH if req.ev_kwh_needed is None else req.ev_kwh_needed,
                           req.earliest_start, req.deadline, **options)
        elif req.appliance in config.APPLIANCES:
            plan = plan_appliance(FORECAST, get_appliance(req.appliance),
                                  req.earliest_start, req.deadline, **options)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown appliance: {req.appliance}")
    except (InfeasibleError, ValueError) as error:
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
