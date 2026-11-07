"""Daily historical replay with realised appliance receipts and battery P&L.

Run with python -m src.backtest. Models are fitted before the calibration and
test periods. Rolling calibration may consume only already-published outcomes.
"""

import argparse
import json
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import config
from src import baselines, calibration, models, optimiser


def next_local_time(cutoff, hour):
    """Next occurrence of a UK clock time, including clock-change days."""
    local = cutoff.tz_convert(config.UK_TIMEZONE).to_pydatetime().replace(tzinfo=None)
    result = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if result <= local:
        result += timedelta(days=1)
    return pd.Timestamp(result).tz_localize(config.UK_TIMEZONE).tz_convert("UTC")


def point_table(index, price, carbon):
    table = pd.DataFrame(index=pd.DatetimeIndex(index, name="time_utc"))
    for quantile in ("p10", "p50", "p90"):
        table[f"price_{quantile}"] = np.asarray(price)
        table[f"carbon_{quantile}"] = np.asarray(carbon)
    table["price_published"] = False
    return table


def realised_totals(actual, slots, energy):
    """Score a chosen schedule against actuals, never forecast cost estimates."""
    values = actual.loc[slots, ["price_p50", "carbon_p50"]].to_numpy()
    if not np.isfinite(values).all():
        raise ValueError("Missing actuals for the chosen schedule.")
    totals = np.asarray(energy) @ values
    return float(totals[0] / 100), float(totals[1] / 1000)


def prepare_forecasts(features, days, model_dir, estimators):
    """Refit before the requested holdout instead of reusing overlapping models."""
    if days < 1:
        raise ValueError("days must be positive.")
    if features.duplicated(["forecast_at_utc", "target_at_utc"]).any():
        raise ValueError("Duplicate issue/target feature rows.")
    if "availability_mode" in features:
        modes = set(features.availability_mode.dropna())
        if modes not in ({"buffered"}, {"recorded"}):
            raise ValueError("Use a single supported feature availability policy.")
    train, valid, test = models.chronological_split(features, test_days=days)
    model_dir.mkdir(parents=True, exist_ok=True)
    for source, (target, _) in models.TARGETS.items():
        print(f"Fitting backtest {source} models before the {days}-day holdout ...", flush=True)
        fitted = models.fit_quantiles(train, target, estimators)
        for quantile, model in fitted.items():
            model.booster_.save_model(str(model_dir / f"{source}_p{int(100 * quantile)}.txt"))
    (model_dir / "training.json").write_text(json.dumps({
        "last_training_cutoff": str(pd.to_datetime(train.forecast_at_utc, utc=True).max())
    }) + "\n")
    tables = calibration.rebuild_predictions(features, model_dir, test_days=days)
    wide = test[["forecast_at_utc", "target_at_utc", "horizon_minutes"]].copy()
    for source, table in tables.items():
        fixed = calibration.fixed_offsets(table[table["split"] == "validation"])
        calibrated, _ = calibration.calibrate_rolling(
            table, table[table["split"] == "test"], fixed, calibration.FLOORS[source]
        )
        for quantile in ("p10", "p50", "p90"):
            wide[f"{source}_{quantile}"] = calibrated[quantile]
    chronology = {
        "last_training_cutoff": str(train.forecast_at_utc.max()),
        "first_calibration_cutoff": str(valid.forecast_at_utc.min()),
        "first_test_cutoff": str(test.forecast_at_utc.min()),
        "last_test_cutoff": str(test.forecast_at_utc.max()),
        "purge_hours": 52,
    }
    return wide, chronology


def appliance_receipts(cutoff, forecast, actual, yesterday, carbon_weight, risk_aversion):
    """One run per configured appliance daily, finish by next 07:00 UK time."""
    deadline = next_local_time(cutoff, 7)
    overnight = next_local_time(cutoff, 0)
    rows = []
    for key in config.APPLIANCES:
        appliance = optimiser.get_appliance(key)
        for strategy in ("run_now", "fixed_overnight", "yesterday", "model", "perfect_foresight"):
            if strategy in ("run_now", "fixed_overnight"):
                start = cutoff if strategy == "run_now" else overnight
                slots = list(pd.date_range(start, periods=appliance.duration_slots, freq="30min"))
                energy = [appliance.energy_per_slot_kwh] * len(slots)
                if slots[-1] + optimiser.SLOT > deadline:
                    raise ValueError("Fixed schedule exceeds the household deadline.")
            else:
                inputs = {"model": forecast, "yesterday": yesterday,
                          "perfect_foresight": actual.loc[actual.index < deadline]}
                # Perfect foresight is the money-saving upper bound, regardless of slider.
                weight = 0.0 if strategy == "perfect_foresight" else carbon_weight
                risk = risk_aversion if strategy == "model" else 0.0
                plan = optimiser.plan_appliance(
                    inputs[strategy], appliance, cutoff, deadline,
                    carbon_weight=weight, risk_aversion=risk, alternatives=0,
                )
                slots, energy = plan.slots, plan.slot_energy_kwh
            cost, carbon = realised_totals(actual, slots, energy)
            rows.append({
                "forecast_at_utc": cutoff, "appliance": key, "strategy": strategy,
                "start_utc": slots[0], "end_utc": slots[-1] + optimiser.SLOT,
                "energy_kwh": sum(energy), "cost_gbp": cost, "co2_kg": carbon,
            })
    return rows


def battery_receipt(cutoff, forecast, actual, initial, carbon_weight, risk_aversion):
    """Execute until next daily cutoff and carry state of charge between days."""
    end = next_local_time(cutoff, config.FORECAST_HOUR_LOCAL)
    forecast = forecast.loc[forecast.index < end]
    actual = actual.loc[forecast.index]
    plan = optimiser.plan_battery(
        forecast, initial_kwh=initial, carbon_weight=carbon_weight, risk_aversion=risk_aversion
    )
    schedule = plan.schedule
    simultaneous = (schedule.charge_kwh > 1e-5) & (schedule.discharge_kwh > 1e-5)
    if simultaneous.any():
        raise ValueError("Battery LP produced simultaneous charge/discharge; cannot publish physical receipts.")
    final = float(schedule.stored_kwh.iloc[-1])
    if final < initial - 1e-5:
        raise ValueError("Battery terminal charge constraint violated.")
    base_cost, base_carbon = realised_totals(actual, schedule.index, schedule.load_kwh)
    cost, carbon = realised_totals(actual, schedule.index, schedule.grid_kwh)
    wear = float((schedule.charge_kwh + schedule.discharge_kwh).sum()
                 * config.BATTERY["degradation_p_per_kwh"] / 100)
    record = {
        "forecast_at_utc": cutoff, "initial_kwh": initial, "final_kwh": final,
        "without_battery_gbp": base_cost, "with_battery_gbp": cost,
        "degradation_gbp": wear, "pnl_gbp": base_cost - cost - wear,
        "saving_co2_kg": base_carbon - carbon,
    }
    return record, schedule.assign(forecast_at_utc=cutoff), final


def pnl_metrics(values):
    """GBP drawdown from initial zero equity; Sharpe of daily GBP savings."""
    values = np.asarray(values, dtype=float)
    equity = np.r_[0.0, np.cumsum(values)]
    deviation = np.std(values, ddof=1) if len(values) > 1 else 0.0
    return {
        "net_pnl_gbp": float(values.sum()),
        "annualised_daily_savings_sharpe": float(np.sqrt(365) * values.mean() / deviation)
        if deviation > 1e-12 else None,
        "max_drawdown_gbp": float(np.max(np.maximum.accumulate(equity) - equity)),
    }


def summarise(receipts):
    daily = receipts.groupby(["forecast_at_utc", "strategy"])[["cost_gbp", "co2_kg"]].sum().reset_index()
    base = daily[daily.strategy == "run_now"].set_index("forecast_at_utc")
    for unit in ("gbp", "kg"):
        column = "cost_gbp" if unit == "gbp" else "co2_kg"
        daily[f"saving_{unit}"] = daily.forecast_at_utc.map(base[column]) - daily[column]
        daily[f"cumulative_saving_{unit}"] = daily.groupby("strategy")[f"saving_{unit}"].cumsum()
    totals = daily.groupby("strategy")[["cost_gbp", "co2_kg", "saving_gbp", "saving_kg"]].sum()
    upper = totals.loc["perfect_foresight", "saving_gbp"]
    totals["perfect_foresight_saving_captured_pct"] = (
        100 * totals.saving_gbp / upper if upper > 1e-9 else np.nan
    )
    return daily, totals.reset_index()


def plot_receipts(daily, battery, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    for strategy, rows in daily.groupby("strategy"):
        axes[0].plot(rows.forecast_at_utc, rows.cumulative_saving_gbp,
                     label=strategy.replace("_", " "))
    axes[0].set(title="Appliance savings versus running immediately", ylabel="Cumulative GBP")
    axes[0].legend(fontsize=8)
    axes[1].plot(battery.forecast_at_utc, battery.pnl_gbp.cumsum(), color="#287957")
    axes[1].set(title="Battery savings after degradation versus no battery", ylabel="Cumulative GBP")
    for axis in axes:
        axis.axhline(0, color="gray", linewidth=0.7)
        axis.tick_params(axis="x", rotation=30)
        axis.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def run(features, dataset, *, out_dir=Path("results/step8"), days=90,
        carbon_weight=0.0, risk_aversion=0.5, estimators=160):
    if not 0 <= carbon_weight <= 1 or not np.isfinite(risk_aversion) or risk_aversion < 0:
        raise ValueError("Use carbon_weight in [0, 1] and finite non-negative risk_aversion.")
    out_dir = Path(out_dir)
    forecast_rows, chronology = prepare_forecasts(features, days, out_dir / "models", estimators)
    cutoffs = pd.DatetimeIndex(forecast_rows.forecast_at_utc.unique()).sort_values()
    if any(next_local_time(first, config.FORECAST_HOUR_LOCAL) != second
           for first, second in zip(cutoffs[:-1], cutoffs[1:])):
        raise ValueError("Daily replay cutoffs must be consecutive to carry battery state.")
    baseline_rows = baselines.make_predictions(dataset, cutoffs)
    baseline_groups = baseline_rows.groupby("forecast_at_utc")
    receipts, batteries, schedules, skipped = [], [], [], []
    initial = config.BATTERY["initial_kwh"]
    for cutoff, group in forecast_rows.groupby("forecast_at_utc", sort=True):
        group = group.sort_values("target_at_utc")
        index = pd.DatetimeIndex(group.target_at_utc, name="time_utc")
        forecast = group.set_index(index)[list(optimiser.PRICE_BAND + optimiser.CARBON_BAND)].copy()
        forecast["price_published"] = False
        optimiser.validate_forecast(forecast)
        baseline = baseline_groups.get_group(cutoff).set_index("target_at_utc").reindex(index)
        actual = point_table(index, baseline.actual_price, baseline.actual_carbon)
        deadline = next_local_time(cutoff, 7)
        household = baseline.loc[index < deadline]
        if not np.isfinite(household[["price_yesterday", "carbon_yesterday"]]).all().all():
            skipped.append({"forecast_at_utc": str(cutoff), "reason": "Incomplete yesterday baseline before deadline"})
        elif not np.isfinite(actual.loc[index < deadline, ["price_p50", "carbon_p50"]]).all().all():
            skipped.append({"forecast_at_utc": str(cutoff), "reason": "Incomplete actuals before deadline"})
        else:
            yesterday = point_table(household.index, household.price_yesterday, household.carbon_yesterday)
            receipts.extend(appliance_receipts(cutoff, forecast, actual, yesterday, carbon_weight, risk_aversion))
        battery, schedule, initial = battery_receipt(
            cutoff, forecast, actual, initial, carbon_weight, risk_aversion
        )
        batteries.append(battery)
        schedules.append(schedule)
    if not receipts:
        raise ValueError("No complete paired household days to evaluate.")
    receipts = pd.DataFrame(receipts)
    battery = pd.DataFrame(batteries)
    daily, summary = summarise(receipts)
    metadata = {
        "requested_days": days, "evaluated_appliance_days": int(receipts.forecast_at_utc.nunique()),
        "evaluated_battery_days": len(battery), "skipped_appliance_days": skipped,
        "chronology": chronology, "carbon_weight": carbon_weight, "risk_aversion": risk_aversion,
        "battery": pnl_metrics(battery.pnl_gbp),
        "assumptions": [
            "Simulated household runs each configured appliance once daily, available at 15:00 UK, due next 07:00. Fixed overnight starts at midnight.",
            "Separate battery household uses config's synthetic load, carries charge between daily plans, and ends each plan with at least its starting charge. No export or capital cost.",
            "Frozen models are newly fitted before the 90-day holdout (or requested days); rolling calibration uses only labels available at each cutoff.",
            "Historical weather/carbon availability uses Step 3 release-buffer assumptions, not verified issue logs.",
            "Future Agile prices are treated as unknown because historical publication timestamps are absent. This is a forecast-only scenario.",
            "Carbon actuals are GB-wide. Official archived forecast corrections are excluded.",
            "Perfect foresight minimises actual appliance cost with the same constraints; it is a money-saving upper bound, not a carbon bound.",
            "Battery Sharpe uses daily GBP savings relative to no battery and sqrt(365), not investment returns. Correlated daily savings limit interpretation.",
        ],
    }
    receipts.to_csv(out_dir / "appliance_receipts.csv", index=False)
    daily.to_csv(out_dir / "daily_savings.csv", index=False)
    summary.to_csv(out_dir / "summary.csv", index=False)
    battery.to_csv(out_dir / "battery_daily.csv", index=False)
    pd.concat(schedules).to_csv(out_dir / "battery_schedule.csv", index_label="time_utc")
    forecast_rows.to_csv(out_dir / "forecasts.csv", index=False)
    (out_dir / "summary.json").write_text(json.dumps(metadata, indent=2, allow_nan=False) + "\n")
    plot_receipts(daily, battery, out_dir / "cumulative_savings.png")
    print(summary.to_string(index=False, float_format=lambda value: f"{value:.3f}"))
    print(json.dumps(metadata["battery"], indent=2))
    print(f"Saved receipts for {metadata['evaluated_appliance_days']} paired household days to {out_dir}")
    return summary, metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, default=config.DATA_DIR / "features.parquet")
    parser.add_argument("--dataset", type=Path, default=config.DATA_DIR / "dataset.parquet")
    parser.add_argument("--out-dir", type=Path, default=Path("results/step8"))
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--carbon-weight", type=float, default=0.0)
    parser.add_argument("--risk-aversion", type=float, default=0.5)
    args = parser.parse_args()
    run(models._read_table(args.features), models._read_table(args.dataset),
        out_dir=args.out_dir, days=args.days,
        carbon_weight=args.carbon_weight, risk_aversion=args.risk_aversion)


if __name__ == "__main__":
    main()
