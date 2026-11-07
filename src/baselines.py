"""Leakage-safe yesterday/last-week forecast baselines for Agile price and GB carbon.

Each forecast is made at 15:00 Europe/London and covers the following 24–48h.
The reference reading must have become available by the forecast cutoff.
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import config
from src.build_dataset import CARBON, PRICE

FORECAST_HOUR_LOCAL = getattr(config, "FORECAST_HOUR_LOCAL", 15)
FORECAST_HORIZON_HOURS = getattr(config, "FORECAST_HORIZON_HOURS", 48)
CARBON_RELEASE_BUFFER_HOURS = getattr(config, "CARBON_RELEASE_BUFFER_HOURS", 2)
SLOT = pd.Timedelta(minutes=30)
BASELINES = {"yesterday": 1, "last_week": 7}
TARGETS = {"price": (PRICE, "p/kWh"), "carbon": (CARBON, "gCO2/kWh")}


def _history(dataset):
    """Require unique, UTC-aware, half-hour observations; reject ambiguous input."""
    if "time_utc" not in dataset:
        raise ValueError("Dataset needs a time_utc column.")
    missing = {PRICE, CARBON} - set(dataset.columns)
    if missing:
        raise ValueError(f"Dataset missing columns: {', '.join(sorted(missing))}")
    timestamps = pd.DatetimeIndex(pd.to_datetime(dataset["time_utc"], utc=True))
    if timestamps.hasnans or timestamps.has_duplicates or not timestamps.equals(timestamps.floor("30min")):
        raise ValueError("time_utc must contain unique UTC half-hour timestamps.")
    return dataset.set_index(timestamps).sort_index()


def forecast_cutoffs(dataset, horizon_hours=48):
    """Daily 15:00 UK cutoffs with a complete target horizon in the dataset."""
    if not isinstance(horizon_hours, int) or not 1 <= horizon_hours <= 48:
        raise ValueError("horizon_hours must be a whole number between 1 and 48.")
    times = _history(dataset).index
    if times.empty:
        return pd.DatetimeIndex([], tz="UTC")
    local = times.tz_convert(config.UK_TIMEZONE)
    days = pd.date_range(local.min().date(), local.max().date(), freq="D")
    cutoffs = (days + pd.Timedelta(hours=FORECAST_HOUR_LOCAL)).tz_localize(
        config.UK_TIMEZONE
    ).tz_convert("UTC")
    last_cutoff = times.max() - pd.Timedelta(hours=horizon_hours) + SLOT
    return cutoffs[(cutoffs >= times.min()) & (cutoffs <= last_cutoff)]


def _previous_local_day(targets, days):
    """Match the local clock, even across DST. Spring gap -> missing reference."""
    earlier = targets.tz_convert(config.UK_TIMEZONE).tz_localize(None) - pd.Timedelta(days=days)
    # When autumn's hour repeats, choose the second occurrence consistently.
    return earlier.tz_localize(
        config.UK_TIMEZONE, ambiguous=False, nonexistent="NaT"
    ).tz_convert("UTC")


def _source_ready_times(history, source):
    """Availability times are never earlier than the end of the delivery slot.

    Agile is conservatively treated as available after delivery, even though
    the day-ahead tariff often becomes known earlier. NESO actual carbon uses
    a recorded publication time where present; otherwise we ASSUME a 2h lag
    after the half-hour ends. This assumption is not an audited publication log.
    """
    slot_end = pd.Series(history.index + SLOT, index=history.index)
    if source == "price":
        return slot_end
    field = "gb_carbon_actual_published_at_utc"
    recorded = pd.to_datetime(history[field], format="ISO8601", utc=True) if field in history else pd.Series(pd.NaT, index=history.index, dtype="datetime64[ns, UTC]")
    assumed = slot_end + pd.Timedelta(hours=CARBON_RELEASE_BUFFER_HOURS)
    return recorded.fillna(assumed).where(lambda x: x >= slot_end, slot_end)


def make_predictions(dataset, cutoffs, horizon_hours=48):
    """Return observed labels and reference values known by each cutoff.

    A strict 'yesterday' reference is deliberately missing for later 48h
    targets: using the real tomorrow/next-day value would leak the future.
    """
    if not isinstance(horizon_hours, int) or not 1 <= horizon_hours <= 48:
        raise ValueError("horizon_hours must be a whole number between 1 and 48.")
    history = _history(dataset)
    issues = pd.DatetimeIndex(cutoffs)
    if issues.tz is None or issues.hasnans or issues.has_duplicates:
        raise ValueError("Cutoffs must be unique, timezone-aware timestamps.")
    issues = issues.tz_convert("UTC").sort_values()
    minutes = np.arange(0, 60 * horizon_hours, 30)
    issued_at = issues.repeat(len(minutes))
    target_at = issued_at + pd.to_timedelta(np.tile(minutes, len(issues)), unit="m")
    result = pd.DataFrame({
        "forecast_at_utc": issued_at,
        "target_at_utc": target_at,
        "horizon_minutes": np.tile(minutes, len(issues)),
    })

    lag_times = {label: _previous_local_day(target_at, days) for label, days in BASELINES.items()}
    for name, (column, _) in TARGETS.items():
        values = pd.to_numeric(history[column], errors="coerce")
        if name == "carbon" and "gb_carbon_actual_suspect" in history:
            values = values.mask(history["gb_carbon_actual_suspect"].eq(True))
        result[f"actual_{name}"] = values.reindex(target_at).to_numpy(dtype=float)
        ready = _source_ready_times(history, name)
        for lag, earlier in lag_times.items():
            reference = values.reindex(earlier).to_numpy(dtype=float)
            available_at = ready.reindex(earlier)
            known = np.asarray(available_at.array <= issued_at, dtype=bool)
            reference[~known] = np.nan
            result[f"{name}_{lag}"] = reference
    return result


def score_baselines(predictions):
    """Calculate MAE and RMSE on SAME valid target slots for both baselines.

    Coverage is also reported independently, since a strict yesterday baseline
    cannot serve all 48h horizons at forecast time.
    """
    rows = []
    for source, (_, units) in TARGETS.items():
        actual = predictions[f"actual_{source}"].to_numpy(dtype=float)
        valid_labels = np.isfinite(actual)
        predicted = {name: predictions[f"{source}_{name}"].to_numpy(dtype=float) for name in BASELINES}
        common = valid_labels & np.logical_and.reduce([np.isfinite(x) for x in predicted.values()])
        for name, values in predicted.items():
            independent = valid_labels & np.isfinite(values)
            error = values[common] - actual[common]
            rows.append({
                "target": source,
                "baseline": name,
                "unit": units,
                "mae": float(np.mean(np.abs(error))) if len(error) else np.nan,
                "rmse": float(np.sqrt(np.mean(error ** 2))) if len(error) else np.nan,
                "common_slots": int(common.sum()),
                "available_slots": int(independent.sum()),
                "total_labeled_slots": int(valid_labels.sum()),
                "coverage_pct": float(100 * independent.sum() / valid_labels.sum()) if valid_labels.any() else np.nan,
            })
    return pd.DataFrame(rows)


def _load_dataset(path):
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    if path.suffix.lower() == ".parquet":
        try:
            return pd.read_parquet(path)
        except ImportError as exc:
            raise SystemExit("Reading Parquet requires pyarrow: python -m pip install pyarrow") from exc
    raise SystemExit("Input must be a .parquet or .csv file.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=config.DATA_DIR / "dataset.parquet")
    parser.add_argument("--horizon-hours", type=int, default=FORECAST_HORIZON_HOURS)
    parser.add_argument("--holdout-days", type=int, default=60,
                        help="Number of most recent daily forecast cutoffs to evaluate.")
    parser.add_argument("--output", type=Path, default=config.DATA_DIR / "baseline_metrics.csv")
    args = parser.parse_args()
    if not args.input.is_file():
        parser.error(f"No input dataset at {args.input}. Run Step 2 first.")
    if args.holdout_days < 1:
        parser.error("--holdout-days must be positive.")
    data = _load_dataset(args.input)
    cutoffs = forecast_cutoffs(data, args.horizon_hours)
    if len(cutoffs) < args.holdout_days + 1:
        parser.error("Not enough daily cutoffs for this holdout; use fewer days or more history.")
    test_cutoffs = cutoffs[-args.holdout_days:]
    predictions = make_predictions(data, test_cutoffs, args.horizon_hours)
    scores = score_baselines(predictions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    scores.to_csv(args.output, index=False, float_format="%.4f")
    print(f"Holdout: {test_cutoffs[0]} to {test_cutoffs[-1]} (UTC); {len(test_cutoffs)} daily cutoffs")
    print(f"Forecast window: {args.horizon_hours}h, at {FORECAST_HOUR_LOCAL:02}:00 Europe/London")
    print("Fair comparison: both baselines scored on identical available slots per target.")
    print("Carbon actual availability: recorded publication time, or assumed 2h after delivery.")
    print(scores.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
    print(f"Saved metrics to {args.output}")


if __name__ == "__main__":
    main()
