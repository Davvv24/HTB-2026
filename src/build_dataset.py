"""Build the historical half-hour dataset and its sanity plots."""

import argparse
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile

import pandas as pd

import config
from src import history

PRICE = "price_p_kwh_inc_vat"
CARBON = "gb_carbon_actual_gco2_kwh"
OFFICIAL = "gb_carbon_forecast_archive_gco2_kwh"
REGIONAL = "south_scotland_carbon_forecast_archive_gco2_kwh"
DEMAND = "demand_mw"
FUELS = ("biomass", "coal", "imports", "gas", "nuclear", "other", "hydro", "solar", "wind")


def _half_hours(name, time_key, columns, start, end, index):
    frame = history.read_history(name, time_key, start, end)
    frame = frame.drop_duplicates("time_utc", keep="last").set_index("time_utc")
    return frame.reindex(index=index, columns=list(columns)).rename(columns=columns)


def _weather_hours(name, fields, prefix, start, end, index):
    locations = [point[0] for point in config.WEATHER_POINTS]
    columns = pd.MultiIndex.from_product([fields, locations])
    raw = history.read_history(name, "time_utc", start, end)
    raw = raw.reindex(columns=["time_utc", "location", *fields])
    raw = raw.drop_duplicates(["time_utc", "location"], keep="last")
    wide = raw.pivot(index="time_utc", columns="location", values=list(fields))
    # Each half-hour uses its own UTC hour. A missing hour stays missing.
    aligned = wide.reindex(index=index.floor("h"), columns=columns)
    aligned.index = index
    aligned.columns = [
        f"{prefix}_{site.lower().replace(' ', '_')}_{field}" for field, site in columns
    ]
    return aligned.astype("float64")


def build_dataset(start, end):
    """Join source records to one row per half-hour in [start, end) UTC."""
    index = pd.date_range(start, end, freq="30min", inclusive="left", name="time_utc")
    price = _half_hours("octopus_agile", "valid_from", {
        PRICE: PRICE,
        "tariff_code": "price_tariff_code",
    }, start, end, index)
    carbon = _half_hours("neso_gb_history", "from", {
        "actual_gco2_kwh": CARBON,
        "forecast_gco2_kwh": OFFICIAL,
    }, start, end, index)
    regional = _half_hours("neso_regional_history", "from", {
        "forecast_gco2_kwh": REGIONAL,
        **{f"{fuel}_pct": f"south_scotland_{fuel}_pct" for fuel in FUELS},
    }, start, end, index)
    mix = _half_hours("neso_generation_mix", "from", {
        f"{fuel}_pct": f"gb_{fuel}_pct" for fuel in FUELS
    }, start, end, index)
    # Keep the published value and flag an inconsistent zero for review.
    carbon["gb_carbon_actual_suspect"] = carbon[CARBON].eq(0) & mix["gb_gas_pct"].gt(0)
    demand = _half_hours("elexon_demand", "startTime", {
        "initialDemandOutturn": DEMAND,
        "initialTransmissionSystemDemandOutturn": "transmission_demand_mw",
        "publishTime": "demand_published_at_utc",
    }, start, end, index)
    demand["demand_published_at_utc"] = pd.to_datetime(
        demand["demand_published_at_utc"], format="ISO8601", utc=True
    )

    forecasts = _weather_hours(
        "weather_previous_runs", history.PREVIOUS_RUN_FIELDS, "weather_forecast", start, end, index
    )
    for lead in config.WEATHER_LEAD_DAYS:
        columns = [col for col in forecasts if col.endswith(f"_previous_day{lead}")]
        has_forecast = forecasts[columns].notna().any(axis=1)
        # The archive labels a lead time. Apply a release buffer before allowing feature use.
        available = pd.Series(
            index.floor("h") - pd.Timedelta(days=lead)
            + pd.Timedelta(hours=config.WEATHER_RELEASE_BUFFER_HOURS),
            index=index,
        )
        forecasts[f"weather_day{lead}_assumed_available_at_utc"] = available.where(has_forecast)

    reanalysis = _weather_hours(
        "weather_archive", config.WEATHER_FIELDS, "era5", start, end, index
    )
    return pd.concat([price, carbon, regional, mix, demand, forecasts, reanalysis], axis=1)


def _print_stats(frame):
    print(f"\n{len(frame):,} UTC half-hours from {frame.index[0]} through {frame.index[-1]}.")
    for column, label in (
        (PRICE, "Agile price (p/kWh)"),
        (CARBON, "GB actual carbon (gCO₂/kWh)"),
        (OFFICIAL, "GB archived forecast"),
        (REGIONAL, "South Scotland archived forecast"),
        ("gb_wind_pct", "GB wind share (%)"),
        (DEMAND, "Demand (MW)"),
    ):
        values = frame[column].dropna()
        stats = f", min {values.min():.2f}, mean {values.mean():.2f}, max {values.max():.2f}" if len(values) else ""
        print(f"  {label}  {len(values):,}/{len(frame):,} present ({len(values) / len(frame):.2%}){stats}")

    suspect = frame["gb_carbon_actual_suspect"].sum()
    if suspect:
        print(f"  GB actual carbon flagged as zero with gas present  {suspect:,} slots")

    for prefix, label in (("weather_forecast_", "GFS forecasts"), ("era5_", "ERA5 reanalysis")):
        values = frame.filter(like=prefix)
        coverage = values.notna().sum().sum() / values.size
        print(f"  {label}  {coverage:.2%} of site-variable values present")


def plot_sanity(frame, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    recent = frame.tail(14 * 48)
    time = recent.index
    fig, axes = plt.subplots(4, 1, figsize=(12, 9), sharex=True, layout="constrained")
    fig.suptitle("Grid Advisor | Historical data checks", fontsize=18, fontweight="bold")

    axes[0].plot(time, recent[PRICE], color="#214e9e", linewidth=1.1)
    axes[0].axhline(0, color="#adb8c9", linewidth=0.8)
    axes[0].set_title("South Scotland Agile import rate", loc="left")
    axes[0].set_ylabel("p/kWh incl. VAT")

    axes[1].plot(time, recent[CARBON], color="#13776a", label="GB actual", linewidth=1.1)
    axes[1].plot(time, recent[OFFICIAL], color="#b9652e", linestyle="--",
                 label="NESO historical forecast", linewidth=1)
    axes[1].set_title("GB carbon intensity by valid time", loc="left")
    axes[1].set_ylabel("gCO₂/kWh")
    axes[1].legend(loc="upper left", frameon=False, ncols=2)

    axes[2].plot(time, recent[DEMAND] / 1_000, color="#6d4ba0", linewidth=1.1)
    axes[2].set_title("GB initial demand outturn", loc="left")
    axes[2].set_ylabel("GW")

    for lead in config.WEATHER_LEAD_DAYS:
        key = f"weather_forecast_south_scotland_wind_speed_100m_previous_day{lead}"
        axes[3].plot(time, recent[key], label=f"GFS {24 * lead}h lead", linewidth=1)
    axes[3].plot(time, recent["era5_south_scotland_wind_speed_100m"], color="#344455",
                 linestyle=":", label="ERA5", linewidth=1)
    axes[3].set_title("South Scotland wind at 100 m", loc="left")
    axes[3].set_ylabel("m/s")
    axes[3].legend(loc="upper right", frameon=False, ncols=4)

    for axis in axes:
        axis.grid(axis="y", color="#e2e8f0")
        axis.spines[["top", "right"]].set_visible(False)
    locator = mdates.AutoDateLocator()
    axes[-1].xaxis.set_major_locator(locator)
    axes[-1].xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    axes[-1].set_xlabel("UTC")
    fig.savefig(path, dpi=155)
    plt.close(fig)


def _write_parquet(frame, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=output.parent, suffix=".tmp", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        frame.reset_index().to_parquet(path, index=False)
        path.replace(output)
    finally:
        path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", type=int, choices=range(12, 25), default=config.HISTORY_MONTHS)
    parser.add_argument("--start", type=date.fromisoformat, help="Inclusive UTC date (YYYY-MM-DD).")
    parser.add_argument("--end", type=date.fromisoformat, help="Exclusive UTC date (YYYY-MM-DD).")
    parser.add_argument("--offline", action="store_true", help="Use only date-matched CSV caches.")
    parser.add_argument("--output", type=Path, default=config.DATA_DIR / "dataset.parquet")
    args = parser.parse_args()

    today = pd.Timestamp.now(tz="UTC").normalize()
    end = pd.Timestamp(args.end, tz="UTC") if args.end else today - pd.Timedelta(days=1)
    start = pd.Timestamp(args.start, tz="UTC") if args.start else end - pd.DateOffset(months=args.months)
    if not start < end <= today:
        parser.error("Use an ordered window of complete UTC dates.")

    print(f"Historical window {start:%Y-%m-%d} to {end:%Y-%m-%d} UTC (exclusive).")
    if args.offline:
        print("Using date-matched saved data.")
    else:
        archive_end = min(end, today - pd.Timedelta(days=config.ERA5_DELAY_DAYS))
        history.download_history(start, end, archive_end)

    frame = build_dataset(start, end)
    _print_stats(frame)
    if frame[PRICE].isna().all() or frame[CARBON].isna().all():
        print("No date-matched price or GB actual carbon rows. Check the dates or fetch live data.")
        return 1
    _write_parquet(frame, args.output)
    plot_path = args.output.with_name(f"{args.output.stem}_sanity.png")
    plot_sanity(frame, plot_path)
    print(f"Saved {args.output} and {plot_path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
