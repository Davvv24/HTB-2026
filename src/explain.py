"""Step 9: computed recommendation text and LightGBM feature contributions.

Run python -m src.explain for a historical example using the Step 8 artefacts.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import config


def _difference(value, unit, places, lower, higher):
    rounded = round(float(value), places)
    if rounded == 0:
        return f"the same {unit} to the displayed precision"
    action = lower if rounded > 0 else higher
    return f"{abs(rounded):.{places}f} {unit} {action}"


def explain_plan(plan):
    """One sentence supported by the optimiser's computed comparison totals."""
    money = _difference(plan.saving_p / 100, "GBP", 2, "less", "more")
    carbon = _difference(plan.saving_co2_kg, "kg CO2", 3, "less", "more")
    return (f"For this schedule, the estimated cost is {money} and emissions are {carbon} "
            "compared with starting at the earliest available slot.")


def explain_battery(plan):
    money = _difference(plan.saving_p / 100, "GBP", 2, "less", "more")
    carbon = _difference(plan.saving_co2_kg, "kg CO2", 3, "less", "more")
    return (f"The battery plan starts with {plan.next_action} and estimates {money} in cost "
            f"after degradation and {carbon} in emissions compared with the same load without a battery.")


def feature_group(name):
    """Readable groups keep correlated weather inputs together in the UI."""
    if name == "bias":
        return "Model reference value"
    if name == "postprocess":
        return "Non-negative carbon adjustment"
    if name.startswith("price_"):
        return "Recent electricity prices"
    if name.startswith("carbon_"):
        return "Recent carbon intensity"
    if "wind" in name:
        return "Wind forecasts"
    if any(word in name for word in ("solar", "radiation", "cloud")):
        return "Solar and cloud forecasts"
    if "temperature" in name:
        return "Temperature forecasts"
    if "demand" in name:
        return "Demand indicators"
    if name == "horizon_minutes":
        return "Forecast lead time"
    return "Time and calendar"


def median_contributions(fitted, features, *, carbon=False):
    """Explain the actual sorted median, including the carbon floor.

    Independently trained quantiles can cross. Select contributions from the
    model supplying each row's middle prediction, matching predict_quantiles.
    """
    from src import models

    x = models.numeric_features(features)
    if any(list(fitted[q].feature_name()) != list(x.columns) for q in models.QUANTILES):
        raise ValueError("Saved model feature order differs from the feature table.")
    raw = np.column_stack([fitted[q].predict(x) for q in models.QUANTILES])
    contributions = np.stack([fitted[q].predict(x, pred_contrib=True)
                              for q in models.QUANTILES], axis=1)
    expected_shape = (len(x), 3, x.shape[1] + 1)
    if contributions.shape != expected_shape or not np.isfinite(contributions).all():
        raise ValueError("Model contributions have an invalid shape or non-finite values.")
    if not np.allclose(contributions.sum(axis=2), raw, rtol=1e-6, atol=1e-6):
        raise ValueError("Feature contributions do not reconstruct the model predictions.")
    selected = np.argsort(raw, axis=1, kind="stable")[:, 1]
    rows = np.arange(len(x))
    median = raw[rows, selected]
    output = pd.DataFrame(contributions[rows, selected], columns=[*x.columns, "bias"])
    clipped = np.maximum(0, median) if carbon else median
    output["postprocess"] = clipped - median
    output["prediction"] = clipped
    output["target_at_utc"] = pd.to_datetime(features.target_at_utc, utc=True).to_numpy()
    return output.set_index("target_at_utc")


def model_explanations(features, model_dir):
    """Compute once per forecast issuance, then reuse across appliance requests."""
    from src import models

    if features.empty or features.forecast_at_utc.nunique() != 1:
        raise ValueError("Select exactly one non-empty forecast issuance.")
    if features.target_at_utc.duplicated().any():
        raise ValueError("Duplicate target timestamps in the selected issuance.")
    return {source: median_contributions(models.load_models(model_dir, source), features,
                                         carbon=(source == "carbon"))
            for source in models.TARGETS}


def explain_recommendation(plan, forecast, contributions):
    """Add auditable model drivers relative to the earliest-slot schedule.

    Contributions explain the median model difference. Calibration/risk and
    optimiser constraints affect selection separately. Published price slots
    and external medians receive no model attribution.
    """
    energy = np.asarray(plan.slot_energy_kwh, dtype=float)
    if len(energy) != len(plan.slots) or len(energy) == 0 or not np.isfinite(energy).all() or (energy <= 0).any():
        raise ValueError("A positive finite energy value is needed for every scheduled slot.")
    if plan.run_now_start_utc is None:
        raise ValueError("The run-now start is required for a comparison.")
    baseline_slots = pd.date_range(plan.run_now_start_utc, periods=len(energy), freq="30min")
    # Appliances use equal energy each slot. EVs charge at full power and top
    # up the final earliest slot, even when their selected slots are disjoint.
    baseline_energy = np.full(len(energy), energy.max())
    baseline_energy[-1] = energy.sum() - baseline_energy[:-1].sum()
    slots = pd.DatetimeIndex(plan.slots)
    needed = slots.union(baseline_slots)
    details = {}
    for source, divisor, unit in (("price", 100, "GBP"), ("carbon", 1000, "kg CO2")):
        chosen_value = float(energy @ forecast.loc[slots, f"{source}_p50"].to_numpy()) / divisor
        baseline_value = float(baseline_energy @ forecast.loc[baseline_slots, f"{source}_p50"].to_numpy()) / divisor
        expected = (plan.cost_p / 100, plan.run_now_cost_p / 100) if source == "price" else (
            plan.co2_kg, plan.run_now_co2_kg)
        if not np.allclose([chosen_value, baseline_value], expected, rtol=1e-6, atol=1e-6):
            raise ValueError("The plan totals do not match the supplied forecast.")
        if source == "price" and forecast.loc[needed, "price_published"].any():
            details[source] = {"status": "published_prices", "drivers": []}
            continue
        table = contributions[source]
        if not needed.isin(table.index).all():
            raise ValueError("Missing contributions for scheduled or baseline slots.")
        if not np.allclose(table.loc[needed, "prediction"], forecast.loc[needed, f"{source}_p50"],
                           rtol=1e-6, atol=1e-6):
            details[source] = {"status": "different_forecast_median", "drivers": []}
            continue
        columns = table.columns.drop("prediction")
        difference = (energy @ table.loc[slots, columns].to_numpy()
                      - baseline_energy @ table.loc[baseline_slots, columns].to_numpy()) / divisor
        grouped = pd.Series(difference, index=columns).groupby(feature_group).sum()
        grouped = grouped.loc[grouped.abs().sort_values(ascending=False).index]
        details[source] = {
            "status": "explained", "unit": unit,
            "recommended_minus_run_now": float(grouped.sum()),
            "drivers": [{"factor": name, "contribution": float(value)}
                        for name, value in grouped.items()],
        }
    why = explain_plan(plan)
    price_drivers = details["price"]["drivers"]
    if price_drivers and abs(price_drivers[0]["contribution"]) >= 0.005:
        driver = price_drivers[0]
        direction = "higher" if driver["contribution"] > 0 else "lower"
        why = (why[:-1] + f", with {driver['factor'].lower()} contributing "
               f"{abs(driver['contribution']):.2f} GBP {direction} to the model's cost comparison.")
    return {
        "why": why, "model_drivers": details,
        "interpretation": "Signed contributions explain the model's median difference between schedules. "
                          "Negative values lower predicted cost or emissions. They are model associations, "
                          "not causal effects or an explanation of the uncertainty penalty.",
    }


def global_importances(model_dir):
    """Gain shares describe model-wide usage, with no directional claim."""
    from src import models

    rows = []
    for source in models.TARGETS:
        for quantile, fitted in models.load_models(model_dir, source).items():
            gain = fitted.feature_importance(importance_type="gain")
            total = gain.sum()
            for feature, value in zip(fitted.feature_name(), gain):
                rows.append({"target": source, "quantile": quantile, "feature": feature,
                             "group": feature_group(feature), "gain": float(value),
                             "gain_pct": float(100 * value / total) if total else 0.0})
    return pd.DataFrame(rows)


def plot_importances(importance, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    for source, axis in zip(("price", "carbon"), axes):
        grouped = importance[(importance.target == source) & (importance["quantile"] == 0.5)]
        grouped = grouped.groupby("group").gain_pct.sum().sort_values()
        axis.barh(grouped.index, grouped.values, color="#287957")
        axis.set(title=f"{source.title()} p50 model", xlabel="Share of training split gain (%)")
    fig.suptitle("Model-wide feature importance")
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def run(features_path, forecasts_path, model_dir, out_dir, forecast_at=None):
    from src import models, optimiser
    from src.backtest import next_local_time

    forecasts = pd.read_csv(forecasts_path, parse_dates=["forecast_at_utc", "target_at_utc"])
    forecasts["forecast_at_utc"] = pd.to_datetime(forecasts.forecast_at_utc, utc=True)
    forecasts["target_at_utc"] = pd.to_datetime(forecasts.target_at_utc, utc=True)
    cutoff = pd.Timestamp(forecast_at) if forecast_at else forecasts.forecast_at_utc.max()
    if cutoff.tzinfo is None:
        raise ValueError("--forecast-at must include a timezone.")
    cutoff = cutoff.tz_convert("UTC")
    forecast = forecasts.loc[forecasts.forecast_at_utc == cutoff].set_index("target_at_utc").sort_index()
    forecast["price_published"] = False
    optimiser.validate_forecast(forecast)
    features = models._read_table(features_path)
    features = features.loc[pd.to_datetime(features.forecast_at_utc, utc=True) == cutoff]
    contributions = model_explanations(features, model_dir)
    examples = []
    deadline = next_local_time(cutoff, 7)
    for key in [*config.APPLIANCES, "ev"]:
        if key == "ev":
            plan = optimiser.plan_ev(forecast, 20, cutoff, deadline, risk_aversion=0.5)
        else:
            plan = optimiser.plan_appliance(forecast, optimiser.get_appliance(key), cutoff,
                                            deadline, risk_aversion=0.5)
        examples.append({**plan.to_dict(), **explain_recommendation(plan, forecast, contributions)})
    battery = optimiser.plan_battery(forecast, risk_aversion=0.5)
    examples.append({"appliance": "battery", **battery.to_dict()})
    out_dir.mkdir(parents=True, exist_ok=True)
    importance = global_importances(model_dir)
    importance.to_csv(out_dir / "feature_importance.csv", index=False)
    for source, table in contributions.items():
        table.to_csv(out_dir / f"{source}_slot_contributions.csv")
    report = {"forecast_at_utc": cutoff.isoformat(), "historical_demo": True,
              "model_dir": str(model_dir), "carbon_weight": 0.0, "risk_aversion": 0.5,
              "recommendations": examples}
    (out_dir / "recommendations.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    plot_importances(importance, out_dir / "feature_importance.png")
    for example in examples:
        print(f"{example['appliance']}: {example['why']}")
    print(f"Saved historical explanations to {out_dir}")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, default=config.DATA_DIR / "features.parquet")
    parser.add_argument("--forecasts", type=Path, default=Path("results/step8/forecasts.csv"))
    parser.add_argument("--model-dir", type=Path, default=Path("results/step8/models"))
    parser.add_argument("--out-dir", type=Path, default=Path("results/step9"))
    parser.add_argument("--forecast-at", help="UTC issue timestamp, default latest saved issuance")
    args = parser.parse_args()
    run(args.features, args.forecasts, args.model_dir, args.out_dir, args.forecast_at)


if __name__ == "__main__":
    main()
