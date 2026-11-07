"""Build inputs for each forecast cutoff and target half-hour."""

import argparse
from math import ceil
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np
import pandas as pd

import config
from src.build_dataset import CARBON, DEMAND, PRICE

SLOT = pd.Timedelta(minutes=30)
LAGS = (("yesterday", 1), ("last_week", 7))
WINDOWS = (("24h", pd.Timedelta(days=1)), ("7d", pd.Timedelta(days=7)))
WEATHER_COLUMNS = tuple(
    f"weather_forecast_{site.lower().replace(' ', '_')}_{field}"
    for site, _, _ in config.WEATHER_POINTS
    for field in config.WEATHER_FIELDS
)

# Use this list for model inputs. Cutoffs, publication times and labels are metadata.
FEATURE_COLUMNS = (
    "horizon_minutes",
    "hour_of_day",
    "day_of_week",
    "month",
    "is_bank_holiday",
    *(f"{source}_{lag}" for source in ("price", "carbon") for lag, _ in LAGS),
    *(f"{source}_mean_{window}" for source in ("price", "carbon") for window, _ in WINDOWS),
    "demand_last_week_mw",
    *WEATHER_COLUMNS,
    "wind_potential",
    "solar_potential",
    "residual_demand_proxy",
)


def _utc_slots(values, label):
    times = pd.DatetimeIndex(values)
    if times.empty or times.tz is None or times.hasnans or times.has_duplicates:
        raise ValueError(f"{label} must contain unique, timezone-aware timestamps.")
    times = times.tz_convert("UTC")
    if not times.equals(times.floor("30min")):
        raise ValueError(f"{label} must be on half-hour boundaries.")
    return times


def _check_horizon(hours):
    if not isinstance(hours, int) or not 1 <= hours <= config.FORECAST_HORIZON_HOURS:
        raise ValueError(f"Horizon must be a whole number of hours from 1 to {config.FORECAST_HORIZON_HOURS}.")


def daily_forecast_times(dataset, horizon_hours=config.FORECAST_HORIZON_HOURS):
    """Return daily UK-local cutoffs whose target windows fit the history bounds."""
    _check_horizon(horizon_hours)
    slots = _utc_slots(dataset["time_utc"], "History timestamps")
    local = slots.tz_convert(config.UK_TIMEZONE).tz_localize(None)
    days = pd.date_range(local.min().normalize(), local.max().normalize(), freq="D")
    daily = days + pd.Timedelta(hours=config.FORECAST_HOUR_LOCAL)
    cutoffs = daily.tz_localize(config.UK_TIMEZONE).tz_convert("UTC")
    last_start = slots.max() - pd.Timedelta(hours=horizon_hours) + SLOT
    return cutoffs[(cutoffs >= slots.min()) & (cutoffs <= last_start)]


def _published_at(history, name):
    timestamps = history.get(name, pd.Series(pd.NaT, index=history.index))
    return pd.to_datetime(timestamps, format="ISO8601", utc=True)


def _local_lag(targets, days):
    local = targets.tz_convert(config.UK_TIMEZONE).tz_localize(None)
    earlier = local - pd.Timedelta(days=days)
    # The repeated autumn hour uses the second occurrence. A spring gap stays missing.
    return earlier.tz_localize(
        config.UK_TIMEZONE, ambiguous=False, nonexistent="NaT"
    ).tz_convert("UTC")


def _known_value(values, available_at, slots, cutoffs):
    known = available_at.reindex(slots).array <= cutoffs
    return values.reindex(slots).where(known).to_numpy()


def _rolling_means(values, available_at, cutoffs):
    rows = []
    for cutoff in cutoffs:
        history = values.loc[cutoff - WINDOWS[-1][1]:cutoff - SLOT]
        known = history.where(available_at.reindex(history.index).le(cutoff))
        row = {}
        for name, window in WINDOWS:
            # Average available slots in [cutoff - window, cutoff), with minimum coverage.
            recent = known.loc[cutoff - window:]
            minimum = ceil(window / SLOT * config.ROLLING_MIN_FRACTION)
            row[name] = recent.mean() if recent.count() >= minimum else np.nan
        rows.append(row)
    return pd.DataFrame(rows, index=cutoffs)


def _bank_holiday_flags(local_dates, cutoffs, calendar):
    dates = pd.DatetimeIndex(pd.to_datetime(calendar["date"], format="%Y-%m-%d"))
    announced = pd.to_datetime(calendar["announced_at_utc"], format="ISO8601", utc=True)
    release = pd.Series(announced.array, index=dates).reindex(local_dates)
    # Recurring holidays are known in advance. One-off holidays require an announcement.
    holiday = local_dates.isin(dates) & (release.isna().to_numpy() | (release.array <= cutoffs))
    covered = local_dates.year.isin(dates.year.unique())
    return pd.array(np.where(covered, holiday.astype(int), np.nan), dtype="Int8")


def _weather_features(history, targets, cutoffs, assume_availability):
    values = np.full((len(targets), len(WEATHER_COLUMNS)), np.nan)
    lead_hours = np.full(len(targets), np.nan)
    known_at = pd.Series(pd.NaT, index=range(len(targets)), dtype="datetime64[ns, UTC]")
    for lead in sorted(config.WEATHER_LEAD_DAYS):
        release = _published_at(history, f"weather_day{lead}_published_at_utc")
        if assume_availability:
            estimated = _published_at(history, f"weather_day{lead}_assumed_available_at_utc")
            release = release.fillna(estimated)

        fields = [f"{name}_previous_day{lead}" for name in WEATHER_COLUMNS]
        forecast = history.reindex(index=targets, columns=fields).to_numpy(dtype=float, na_value=np.nan)
        available = release.reindex(targets).reset_index(drop=True)
        # Use the freshest eligible forecast as a group. Individual field gaps stay null.
        choose = np.isnan(lead_hours) & (available.array <= cutoffs) & ~np.isnan(forecast).all(axis=1)
        values[choose] = forecast[choose]
        lead_hours[choose] = 24 * lead
        known_at.loc[choose] = available.loc[choose]

    # GFS site inputs are wind and gusts in m/s, cloud in %, radiation in W/m² and temperature in °C.
    result = pd.DataFrame(values, columns=WEATHER_COLUMNS)
    result["weather_lead_hours"] = lead_hours
    result["weather_available_at_utc"] = known_at
    return result


def build_features(
    dataset, forecast_times, *, horizon_hours=config.FORECAST_HORIZON_HOURS,
    assume_availability=False, bank_holidays=None,
):
    """Return one row per cutoff and target start, with separate model inputs and labels."""
    _check_horizon(horizon_hours)
    slots = _utc_slots(dataset["time_utc"], "History timestamps")
    history = dataset.set_index(slots).sort_index()
    issues = _utc_slots(forecast_times, "Forecast cutoffs").sort_values()
    horizon_minutes = np.arange(0, 60 * horizon_hours, 30)
    cutoffs = issues.repeat(len(horizon_minutes))
    minutes = np.tile(horizon_minutes, len(issues))
    targets = cutoffs + pd.to_timedelta(minutes, unit="min")
    local = targets.tz_convert(config.UK_TIMEZONE)
    calendar = pd.read_csv(config.SCOTLAND_BANK_HOLIDAYS) if bank_holidays is None else bank_holidays

    frame = pd.DataFrame({
        "forecast_at_utc": cutoffs,
        "target_at_utc": targets,
        "horizon_minutes": minutes,  # Elapsed minutes from the cutoff to the target slot start.
        "hour_of_day": local.hour + local.minute / 60,  # Local clock hour, including half-hours.
        "day_of_week": local.dayofweek,  # Monday is 0.
        "month": local.month,  # Local calendar month.
        "is_bank_holiday": _bank_holiday_flags(local.tz_localize(None).normalize(), cutoffs, calendar),
    })

    slot_end = pd.Series(history.index + SLOT, index=history.index)
    price = history[PRICE]
    # The published Agile rate is available by the end of its delivery slot.
    price_ready = slot_end
    suspect = history.get("gb_carbon_actual_suspect", pd.Series(False, index=history.index))
    carbon = history[CARBON].where(~suspect.eq(True))
    carbon_ready = _published_at(history, "gb_carbon_actual_published_at_utc")
    if assume_availability:
        buffered = slot_end + pd.Timedelta(hours=config.CARBON_RELEASE_BUFFER_HOURS)
        carbon_ready = carbon_ready.fillna(buffered)
    # An observed value cannot be used while its delivery slot is still running.
    carbon_ready = carbon_ready.mask(carbon_ready.lt(slot_end), slot_end)
    demand_ready = _published_at(history, "demand_published_at_utc")
    demand_ready = demand_ready.mask(demand_ready.lt(slot_end), slot_end)

    lag_slots = {name: _local_lag(targets, days) for name, days in LAGS}
    for name, series, release in (("price", price, price_ready), ("carbon", carbon, carbon_ready)):
        # Compare the target's local clock time with yesterday and last week.
        for lag, earlier in lag_slots.items():
            frame[f"{name}_{lag}"] = _known_value(series, release, earlier, cutoffs)
        # Rolling means belong to the cutoff and are shared across its target slots.
        rolling = _rolling_means(series, release, issues)
        for window in rolling:
            frame[f"{name}_mean_{window}"] = rolling[window].reindex(cutoffs).to_numpy()

    # Last week's published demand is the reference demand for the target clock time.
    frame["demand_last_week_mw"] = _known_value(
        history[DEMAND], demand_ready, lag_slots["last_week"], cutoffs
    )
    frame = pd.concat([frame, _weather_features(history, targets, cutoffs, assume_availability)], axis=1)

    wind = frame[[name for name in WEATHER_COLUMNS if name.endswith("_wind_speed_100m")]]
    solar = frame[[name for name in WEATHER_COLUMNS
                   if name.endswith("_shortwave_radiation") and "north_sea" not in name]]
    # Wind potential is a bounded cubic score between 3 and 12 m/s across the four sites.
    frame["wind_potential"] = ((wind.clip(lower=3, upper=12) - 3) / 9).pow(3).mean(axis=1, skipna=False)
    # Solar potential scales mean forecast radiation across the three land sites to 0–1.
    frame["solar_potential"] = solar.clip(lower=0, upper=1000).mean(axis=1, skipna=False) / 1000
    # This dimensionless score subtracts renewable potential from reference demand.
    frame["residual_demand_proxy"] = (
        frame["demand_last_week_mw"] / config.RESIDUAL_DEMAND_SCALE_MW
        - frame["wind_potential"] - frame["solar_potential"]
    )

    # Labels are future outcomes. Their release times also constrain future training splits.
    frame["label_price_p_kwh"] = price.reindex(targets).to_numpy()
    frame["label_carbon_gco2_kwh"] = carbon.reindex(targets).to_numpy()
    frame["price_label_available_at_utc"] = price_ready.where(price.notna()).reindex(targets).array
    frame["carbon_label_available_at_utc"] = carbon_ready.where(carbon.notna()).reindex(targets).array
    frame["availability_mode"] = "buffered" if assume_availability else "recorded"
    return frame


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=config.DATA_DIR / "dataset.parquet")
    parser.add_argument("--output", type=Path, default=config.DATA_DIR / "features.parquet")
    parser.add_argument("--horizon-hours", type=int, default=config.FORECAST_HORIZON_HOURS)
    parser.add_argument("--assume-availability", action="store_true",
                        help="Use the documented carbon and archived weather release buffers.")
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("Choose a different path for the feature file.")
    if not args.input.is_file():
        parser.error("Build or extract data/dataset.parquet from Step 2 first.")

    dataset = pd.read_parquet(args.input)
    cutoffs = daily_forecast_times(dataset, args.horizon_hours)
    if cutoffs.empty:
        parser.error("The input has no complete forecast windows for this horizon.")
    frame = build_features(
        dataset, cutoffs, horizon_hours=args.horizon_hours,
        assume_availability=args.assume_availability,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=args.output.parent, suffix=".tmp", delete=False) as file:
        temp = Path(file.name)
    try:
        frame.to_parquet(temp, index=False)
        temp.replace(args.output)
    finally:
        temp.unlink(missing_ok=True)

    print(f"Saved {len(frame):,} rows from {len(cutoffs):,} daily cutoffs to {args.output}.")
    print(f"Model inputs per row  {len(FEATURE_COLUMNS)}")
    print(f"Availability policy  {frame['availability_mode'].iloc[0]}")
    for column in ("price_yesterday", "carbon_yesterday", "carbon_last_week",
                   "weather_available_at_utc", "residual_demand_proxy"):
        print(f"  {column}  {frame[column].notna().sum():,}/{len(frame):,} present")


if __name__ == "__main__":
    main()
