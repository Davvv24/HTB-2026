"""Download historical source data in resumable UTC date chunks."""

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep
from zoneinfo import ZoneInfo

import pandas as pd

import config
from src import data

PREVIOUS_RUN_FIELDS = tuple(
    f"{field}_previous_day{lead}"
    for lead in config.WEATHER_LEAD_DAYS
    for field in config.WEATHER_FIELDS
)
FETCH_ERRORS = (data.DataFetchError, KeyError, IndexError, TypeError, ValueError)


def _chunks(start, end):
    while start < end:
        stop = min(end, start + timedelta(days=config.HISTORY_CHUNK_DAYS))
        yield start, stop
        start = stop


def _retry(fetch, *args, **kwargs):
    try:
        return fetch(*args, **kwargs)
    except data.DataFetchError:
        sleep(1)
        return fetch(*args, **kwargs)


def _agile_windows(start, end):
    """Work backwards through the Agile products offered during the window."""
    while start < end:
        at = end - timedelta(seconds=1)
        product = _retry(data.discover_agile_product, at)
        tariff = _retry(data.resolve_agile_tariff, product["code"])
        product_start = datetime.fromisoformat(product["available_from"]).astimezone(UTC)
        left = max(start, product_start)
        print(f"  Agile {tariff['tariff_code']} for {left:%Y-%m-%d} to {end:%Y-%m-%d}")
        yield left, end, partial(data.fetch_agile_tariff_rates, tariff)
        end = left


def _demand(start, end):
    uk = ZoneInfo(config.UK_TIMEZONE)
    first_day = start.astimezone(uk).date()
    last_day = (end - timedelta(seconds=1)).astimezone(uk).date()
    # Elexon takes local settlement dates. Its timestamps carry UTC offsets.
    return [
        row for row in data.fetch_elexon_demand(first_day, last_day)
        if start <= datetime.fromisoformat(row["startTime"]) < end
    ]


def _weather(source, start, end, **options):
    return data.fetch_weather(
        source,
        start_date=start.date().isoformat(),
        end_date=(end - timedelta(days=1)).date().isoformat(),
        **options,
    )


def _cache_chunk(name, fetch, start, end):
    path = config.HISTORY_CACHE_DIR / name / f"{start:%Y%m%dT%H%M}_{end:%Y%m%dT%H%M}.csv"
    if path.is_file():
        return name, "cached"

    try:
        rows = _retry(fetch, start, end)
        if not rows:
            raise data.DataFetchError("No rows returned")
    except FETCH_ERRORS as exc:
        print(f"  {name} {start:%Y-%m-%d} to {end:%Y-%m-%d} failed. {exc}")
        return name, "failed"

    frame = pd.DataFrame(rows)
    frame["retrieved_at_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as tmp:
        temp_path = Path(tmp.name)
    try:
        frame.to_csv(temp_path, index=False)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
    return name, "downloaded"


def download_history(start, end, archive_end):
    """Fetch price, carbon, mix, demand and weather, reusing complete CSV chunks."""
    sources = {
        "neso_gb_history": data.fetch_carbon,
        "neso_regional_history": partial(data.fetch_carbon, region_id=config.CARBON_REGION_ID),
        "neso_generation_mix": data.fetch_generation_mix,
        "elexon_demand": _demand,
        "weather_previous_runs": partial(
            _weather, "previous_runs", fields=PREVIOUS_RUN_FIELDS, models=config.WEATHER_HISTORY_MODEL
        ),
    }
    jobs = [
        (name, fetch, left, right)
        for left, right in _chunks(start, end)
        for name, fetch in sources.items()
    ]
    jobs.extend(
        ("weather_archive", partial(_weather, "archive", models="era5"), left, right)
        for left, right in _chunks(start, archive_end)
    )
    try:
        for left, right, fetch in _agile_windows(start, end):
            jobs.extend(
                ("octopus_agile", fetch, chunk_start, chunk_end)
                for chunk_start, chunk_end in _chunks(left, right)
            )
    except FETCH_ERRORS as exc:
        print(f"  Agile catalogue lookup failed. Using dated price caches where available. {exc}")

    counts = defaultdict(Counter)
    with ThreadPoolExecutor(max_workers=config.HISTORY_WORKERS) as pool:
        for name, status in pool.map(lambda job: _cache_chunk(*job), jobs):
            counts[name][status] += 1
    for name, tally in counts.items():
        print(f"  {name}  {tally['downloaded']} downloaded, {tally['cached']} cached, {tally['failed']} failed")


def read_history(name, time_key, start, end):
    """Read only matching timestamps, with the newest retrieval last."""
    paths = [
        config.SAMPLES_DIR / f"{name}.csv",
        config.CACHE_DIR / f"{name}.csv",
        *sorted((config.HISTORY_CACHE_DIR / name).glob("*.csv")),
    ]
    frames = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            frame = pd.read_csv(path)
            times = pd.to_datetime(frame[time_key], format="ISO8601", utc=True)
            frame["retrieved_at_utc"] = pd.to_datetime(
                frame["retrieved_at_utc"], format="ISO8601", utc=True
            )
        except (OSError, ValueError, KeyError) as exc:
            print(f"  Skipping unreadable cache {path}. {exc}")
            continue
        frame["time_utc"] = times
        frames.append(frame.loc[(start <= times) & (times < end)])
    if not frames:
        return pd.DataFrame(columns=["time_utc"])
    frame = pd.concat(frames, ignore_index=True)
    return frame.sort_values("retrieved_at_utc", kind="stable")
