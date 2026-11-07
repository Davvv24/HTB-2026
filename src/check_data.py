"""Probe each public API and show one example from each response."""

import argparse
import csv
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

import config
from src import data


def save_sample(name, rows, retrieved_at):
    path = config.CACHE_DIR / f"{name}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as file:
        writer = csv.DictWriter(file, fieldnames=[*columns, "retrieved_at_utc"])
        writer.writeheader()
        writer.writerows({**row, "retrieved_at_utc": retrieved_at} for row in rows)
    # Publish the sample only after all its rows have been written.
    Path(file.name).replace(path)


def load_sample(name):
    for directory in (config.CACHE_DIR, config.SAMPLES_DIR):
        path = directory / f"{name}.csv"
        try:
            with path.open(encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))
            if rows and rows[0].get("retrieved_at_utc"):
                return rows, path
        except (OSError, csv.Error, UnicodeError):
            continue
    return [], None


def check(name, fetch, offline):
    if not offline:
        try:
            rows = fetch()
            if not rows or not all(isinstance(row, dict) for row in rows):
                raise data.DataFetchError("The response contained no usable rows for this check.")
        except (data.DataFetchError, KeyError, IndexError, TypeError, ValueError) as exc:
            print(f"  {name} live check failed — {exc}")
        else:
            retrieved_at = datetime.now(UTC).isoformat(timespec="seconds")
            try:
                save_sample(name, rows, retrieved_at)
            except OSError as exc:
                print(f"  {name} could not save a cache — {exc}")
            print(f"[LIVE] {name} · {len(rows):,} rows · retrieved {retrieved_at}")
            print(f"  {json.dumps(rows[0], ensure_ascii=False)}")
            return "LIVE"

    rows, path = load_sample(name)
    if not rows:
        print(f"[UNAVAILABLE] {name} · no saved sample")
        return "UNAVAILABLE"

    sample = dict(rows[0])
    retrieved_at = sample.pop("retrieved_at_utc")
    origin = "local cache" if path.parent == config.CACHE_DIR else "bundled sample"
    print(f"[CACHED] {name} · {len(rows):,} rows · {origin} retrieved {retrieved_at}")
    print(f"  {json.dumps(sample, ensure_ascii=False)}")
    return "CACHED"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Use only saved samples.")
    args = parser.parse_args()

    now = datetime.now(UTC)
    slot = now.replace(minute=now.minute // 30 * 30, second=0, microsecond=0)
    # Leave a complete day for recent outturn data to arrive.
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    start = end - timedelta(days=config.CHECK_DAYS)
    london = ZoneInfo(config.UK_TIMEZONE)
    settlement_first = start.astimezone(london).date()
    settlement_last = (end - timedelta(seconds=1)).astimezone(london).date()
    regional = {"region_id": config.CARBON_REGION_ID}
    historical = {
        "start_date": start.date().isoformat(),
        "end_date": (end.date() - timedelta(days=1)).isoformat(),
    }
    # ERA5 reanalysis can arrive several days after the observation.
    archive = {
        "start_date": (start.date() - timedelta(days=7)).isoformat(),
        "end_date": (end.date() - timedelta(days=8)).isoformat(),
        "models": "era5",
    }
    run = start.strftime("%Y-%m-%dT00:00")
    checks = {
        "octopus_agile": lambda: data.fetch_agile_rates(start, end, now),
        "neso_gb_history": lambda: data.fetch_carbon(start, end),
        "neso_gb_forecast": lambda: data.fetch_carbon(slot),
        "neso_regional_history": lambda: data.fetch_carbon(start, end, **regional),
        "neso_regional_forecast": lambda: data.fetch_carbon(slot, **regional),
        "neso_generation_mix": lambda: data.fetch_generation_mix(start, end),
        "elexon_generation": lambda: data.fetch_elexon_generation(settlement_first, settlement_last),
        "elexon_demand": lambda: data.fetch_elexon_demand(settlement_first, settlement_last),
        "elexon_system_prices": lambda: data.fetch_elexon_system_prices(settlement_first, settlement_last),
        "weather_forecast": lambda: data.fetch_weather("forecast", forecast_days=config.CHECK_DAYS),
        "weather_historical_forecast": lambda: data.fetch_weather("historical_forecast", **historical),
        "weather_archive": lambda: data.fetch_weather("archive", **archive),
        "weather_single_run": lambda: data.fetch_weather(
            "single_run", run=run, models="ecmwf_ifs", forecast_days=config.CHECK_DAYS
        ),
    }
    if args.offline:
        print("Offline check. Sample dates are preserved.")
    else:
        print(f"Checking APIs. Recent UTC window {start:%Y-%m-%d} to {end:%Y-%m-%d} (exclusive).")
        print(f"Elexon settlement dates {settlement_first} to {settlement_last} (inclusive).")
    results = Counter(check(name, fetch, args.offline) for name, fetch in checks.items())
    print(f"\n{results['LIVE']} live, {results['CACHED']} cached, {results['UNAVAILABLE']} unavailable.")
    return int(results["UNAVAILABLE"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
