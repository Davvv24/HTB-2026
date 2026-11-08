"""Step 11: today's data for the app, with caching and fallbacks.

forecast  The Step 5 models' 48-hour forecast from the latest 15:00 UK cutoff,
          with Step 6's calibrated bands, and Octopus's published Agile prices
          swapped in wherever they are known (source "model"). Building it
          downloads about 11 days of data, so it runs in a background thread.
          Until it is ready, or if it fails, the app gets published prices plus
          NESO's official carbon forecast (source "published_only").
grid      The latest GB carbon intensity and generation mix from NESO.

Why 15:00: every training example was issued at 15:00 UK time, so the models
have only learned "15:00 + N hours ahead". Each day's forecast is served
until the next 15:00, with published prices refreshed every 10 minutes.

Results are kept in memory for a few minutes and saved under data/live/.
If a provider fails, the last saved copy is used while it still covers now.
If nothing usable exists, the functions return None and the API falls back
to the historical replay.

    python -m src.live --build             build today's model forecast now
    python -m src.live --check 2026-11-03  compare live features with Step 3's

Set GRID_ADVISOR_LIVE=0 to switch live data off, for example for an offline demo.
"""

import argparse
import contextlib
import json
import os
import tempfile
import threading
import time as time_module
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import config
from src import build_dataset, calibration, data, explain, features, history, models, optimiser

LIVE_DIR = config.DATA_DIR / "live"
MODEL_DIR = config.ROOT_DIR / "results/step5"  # Step 6 offsets were measured on these models
OFFSETS_PATH = config.ROOT_DIR / "results/step6/offsets.json"
HISTORY_DAYS = 9  # Last-week lags and 7-day means need 7 days before the cutoff, plus a margin
FORECAST_TTL = timedelta(minutes=10)
GRID_TTL = timedelta(minutes=5)
FAILURE_TTL = timedelta(minutes=1)  # Retry soon after a failed refresh
MIX_SOURCES = ("wind", "gas", "nuclear", "solar", "biomass", "hydro", "imports", "coal", "other")
FETCH_ERRORS = (
    data.DataFetchError, optimiser.ForecastError, KeyError, IndexError, TypeError, ValueError, OSError,
)


def live_enabled():
    return os.environ.get("GRID_ADVISOR_LIVE", "1") != "0"


def iso(when):
    return pd.Timestamp(when).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now(now=None):
    return pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now).tz_convert("UTC")


def _slot(now):
    return now.floor("30min")


def latest_cutoff(now):
    """The most recent 15:00 UK time at or before `now`, in UTC."""
    local = now.tz_convert(config.UK_TIMEZONE)
    day = local.date() if local.hour >= config.FORECAST_HOUR_LOCAL else local.date() - timedelta(days=1)
    cutoff = pd.Timestamp(datetime.combine(day, time(config.FORECAST_HOUR_LOCAL)), tz=config.UK_TIMEZONE)
    return cutoff.tz_convert("UTC")


@dataclass
class ForecastSnapshot:
    table: pd.DataFrame  # Optimiser forecast table, indexed by UTC slot start
    data_as_of: pd.Timestamp  # When the forecast was issued, or the data fetched
    last_updated: pd.Timestamp  # When this server last refreshed it
    source: str  # "model", "published_only" or "historical_demo"
    cached: bool = False
    contributions: dict | None = None  # Step 9 model contributions, for the detailed "why"

    def meta(self):
        """Freshness fields added to every API response that uses the forecast."""
        return {
            "data_as_of": iso(self.data_as_of),
            "last_updated": iso(self.last_updated),
            "cached": self.cached,
            "source": self.source,
            "historical_demo": self.source == "historical_demo",
        }


# --- In-memory cache ---------------------------------------------------------

_lock = threading.Lock()
_memory = {}
_refreshing = set()
_serving = False  # True once the server's refresh loop runs: requests never wait then


def _store(key, now, build):
    value = build(now)
    with _lock:
        _memory[key] = (now, value)
    return value


def _refresh_later(key, now, build):
    """Rebuild `key` in a background thread unless that is already happening."""
    with _lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def run():
        try:
            _store(key, now, build)
        except Exception as exc:
            print(f"Background refresh of {key} failed: {exc}")
        finally:
            with _lock:
                _refreshing.discard(key)

    threading.Thread(target=run, daemon=True, name=f"refresh-{key}").start()


def _remember(key, ttl, now, build):
    """Return the value built within `ttl`, or build a new one.

    In the server, a stale value is returned straight away while a background
    thread fetches a new one, so a request never waits on a provider. A
    request only waits when there is nothing at all to show yet.
    """
    with _lock:
        hit = _memory.get(key)
    if hit is not None:
        built_at, value = hit
        if now - built_at < (ttl if value is not None else FAILURE_TTL):
            return value
        if _serving and value is not None:
            _refresh_later(key, now, build)
            return value
    return _store(key, now, build)


def start_background_refresh(every=timedelta(minutes=5)):
    """Keep the forecast and grid warm. Called once when the API server starts.

    Each pass refreshes published prices and the grid, and starts the daily
    model build as soon as a new 15:00 cutoff passes.
    """
    global _serving
    if not live_enabled():
        return
    with _lock:
        if _serving:
            return
        _serving = True

    def loop():
        while True:
            now = _utc_now()
            for key, build in (("forecast", _refresh_forecast), ("grid", _refresh_grid)):
                try:
                    _store(key, now, build)
                except Exception as exc:
                    print(f"Live refresh of {key} failed: {exc}")
            time_module.sleep(every.total_seconds())

    threading.Thread(target=loop, daemon=True, name="live-refresh").start()


def clear_memory():
    with _lock:
        _memory.clear()
        _models.clear()


# --- Model forecast (Stage B) --------------------------------------------------

_history_lock = threading.Lock()
_models = {}  # cutoff -> ForecastSnapshot built by the model
_building = set()


@contextlib.contextmanager
def _history_cache(directory):
    """Point Step 2's chunk cache at `directory` so live chunks never mix with history."""
    with _history_lock:
        previous = config.HISTORY_CACHE_DIR
        config.HISTORY_CACHE_DIR = Path(directory)
        try:
            yield
        finally:
            config.HISTORY_CACHE_DIR = previous


def live_dataset(cutoff, cache_dir):
    """Step 2's dataset for the days around `cutoff`, fetched into `cache_dir`."""
    start = (cutoff - pd.Timedelta(days=HISTORY_DAYS)).floor("D")
    end = (cutoff + pd.Timedelta(hours=config.FORECAST_HORIZON_HOURS)).ceil("D")
    with _history_cache(cache_dir):
        # archive_end = start skips ERA5 reanalysis, which the models do not use.
        history.download_history(start, end, start)
        # Step 2 keeps time_utc as the index and resets it when writing the file.
        return build_dataset.build_dataset(start, end).reset_index()


def live_features(cutoff, cache_dir):
    """Step 3 features for one 15:00 cutoff, with the same settings as training."""
    dataset = live_dataset(cutoff, cache_dir)
    return features.build_features(dataset, pd.DatetimeIndex([cutoff]), assume_availability=True)


def predict_table(feature_rows, model_dir=MODEL_DIR, offsets_path=OFFSETS_PATH):
    """Step 5 quantiles widened by Step 6's latest rolling offsets, as an optimiser table."""
    offsets = json.loads(Path(offsets_path).read_text())
    buckets = calibration.horizon_bucket(feature_rows["horizon_minutes"])
    labels = [calibration.bucket_label(b) for b in buckets]
    index = pd.DatetimeIndex(pd.to_datetime(feature_rows["target_at_utc"], utc=True), name="time_utc")
    table = pd.DataFrame(index=index)
    for source in ("price", "carbon"):
        predicted = models.predict_quantiles(
            models.load_models(model_dir, source), feature_rows, carbon=(source == "carbon")
        )
        latest = offsets[source]["rolling_latest"]
        lower = np.array([latest[label]["lower"] for label in labels])
        upper = np.array([latest[label]["upper"] for label in labels])
        p10 = np.minimum(predicted[:, 0] - lower, predicted[:, 1])
        p90 = np.maximum(predicted[:, 2] + upper, predicted[:, 1])
        if source == "carbon":
            p10 = np.maximum(p10, 0)
        table[f"{source}_p10"], table[f"{source}_p50"], table[f"{source}_p90"] = p10, predicted[:, 1], p90
    table["price_published"] = False
    return optimiser.validate_forecast(table)


def build_model_forecast(cutoff, now):
    """Download, build features, predict and save the model forecast for `cutoff`."""
    with tempfile.TemporaryDirectory() as cache_dir:
        rows = live_features(cutoff, cache_dir)
    table = predict_table(rows)
    try:
        contributions = explain.model_explanations(rows, MODEL_DIR)
    except Exception as exc:  # The forecast is still useful without detailed reasons
        print(f"Live explanations unavailable: {exc}")
        contributions = None
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    table.to_parquet(LIVE_DIR / "model_forecast.parquet")
    rows.to_parquet(LIVE_DIR / "model_features.parquet", index=False)
    (LIVE_DIR / "model_meta.json").write_text(json.dumps(
        {"cutoff": iso(cutoff), "built_at": iso(now)}, indent=2) + "\n")
    return ForecastSnapshot(table, data_as_of=cutoff, last_updated=now, source="model",
                            contributions=contributions)


def _saved_model():
    """The last saved model forecast, or None."""
    try:
        meta = json.loads((LIVE_DIR / "model_meta.json").read_text())
        table = pd.read_parquet(LIVE_DIR / "model_forecast.parquet")
        table.index = pd.DatetimeIndex(pd.to_datetime(table.index, utc=True), name="time_utc")
    except (OSError, ValueError, KeyError):
        return None
    try:
        contributions = explain.model_explanations(
            pd.read_parquet(LIVE_DIR / "model_features.parquet"), MODEL_DIR)
    except Exception:
        contributions = None
    cutoff = pd.Timestamp(meta["cutoff"])
    return ForecastSnapshot(table, data_as_of=cutoff, last_updated=pd.Timestamp(meta["built_at"]),
                            source="model", cached=True, contributions=contributions)


def _build_in_background(cutoff, now):
    try:
        snapshot = build_model_forecast(cutoff, now)
        with _lock:
            _models[cutoff] = snapshot
            _memory.pop("forecast", None)  # Serve the new forecast on the next request
        print(f"Live model forecast ready for {iso(cutoff)}.")
    except Exception as exc:
        print(f"Live model forecast failed for {iso(cutoff)}: {exc}")
    finally:
        with _lock:
            _building.discard(cutoff)


def model_snapshot(cutoff, now, background=True):
    """The model forecast for `cutoff`, starting a build if needed.

    While it builds, an earlier saved model forecast is returned if one
    exists; otherwise None, so the caller can use published data instead.
    """
    with _lock:
        ready = _models.get(cutoff)
    if ready is not None:
        return ready
    saved = _saved_model()
    if saved is not None and saved.data_as_of == cutoff:
        with _lock:
            _models[cutoff] = saved
        return saved
    with _lock:
        start = cutoff not in _building
        _building.add(cutoff)
    if start:
        if background:
            threading.Thread(target=_build_in_background, args=(cutoff, now), daemon=True).start()
        else:
            _build_in_background(cutoff, now)
            with _lock:
                if cutoff in _models:
                    return _models[cutoff]
    return saved  # Yesterday's forecast still covers most of today, or None


# --- Published data and the combined forecast ----------------------------------


def _published_rows(now):
    slot = _slot(now)
    agile = data.fetch_agile_rates(
        slot.to_pydatetime(), (slot + pd.Timedelta(hours=48)).to_pydatetime(), now.to_pydatetime()
    )
    carbon = data.fetch_carbon(slot.to_pydatetime())
    return agile, carbon


def fetch_published_forecast(now):
    """Published Agile prices and NESO's GB carbon forecast from the current slot."""
    agile, carbon = _published_rows(now)
    table = optimiser.make_forecast_from_published(agile, carbon)
    return ForecastSnapshot(table, data_as_of=now, last_updated=now, source="published_only")


def overlay_published(table, agile_rows):
    """Replace forecast prices with published Agile rates where they exist."""
    table = table.copy()
    prices = pd.Series(
        {pd.Timestamp(row["valid_from"]).tz_convert("UTC"): float(row["price_p_kwh_inc_vat"])
         for row in agile_rows if row.get("price_p_kwh_inc_vat") is not None},
        dtype=float,
    )
    known = table.index.isin(prices.index)
    for column in optimiser.PRICE_BAND:
        table.loc[known, column] = prices.reindex(table.index[known]).to_numpy()
    table.loc[known, "price_published"] = True
    return table


def _save_forecast(snapshot):
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    snapshot.table.to_parquet(LIVE_DIR / "forecast.parquet")
    meta = {"data_as_of": iso(snapshot.data_as_of), "last_updated": iso(snapshot.last_updated),
            "source": snapshot.source}
    (LIVE_DIR / "forecast_meta.json").write_text(json.dumps(meta, indent=2) + "\n")


def _saved_forecast():
    """The last saved served forecast, marked as cached, or None."""
    try:
        table = pd.read_parquet(LIVE_DIR / "forecast.parquet")
        meta = json.loads((LIVE_DIR / "forecast_meta.json").read_text())
        table.index = pd.DatetimeIndex(pd.to_datetime(table.index, utc=True), name="time_utc")
        return ForecastSnapshot(
            table, data_as_of=pd.Timestamp(meta["data_as_of"]),
            last_updated=pd.Timestamp(meta["last_updated"]), source=meta["source"], cached=True,
        )
    except (OSError, ValueError, KeyError) as exc:
        print(f"No usable saved live forecast: {exc}")
        return None


def _refresh_forecast(now, background=True):
    """Model forecast with published prices on top, else published only, else saved."""
    model = model_snapshot(latest_cutoff(now), now, background)
    try:
        agile, carbon = _published_rows(now)
    except FETCH_ERRORS as exc:
        print(f"Published data failed: {exc}")
        agile = carbon = None

    snapshot = None
    if model is not None:
        table = overlay_published(model.table, agile) if agile else model.table
        snapshot = replace(model, table=table, last_updated=now if agile else model.last_updated,
                           cached=model.cached and not agile)
    elif agile is not None:
        try:
            snapshot = ForecastSnapshot(optimiser.make_forecast_from_published(agile, carbon),
                                        data_as_of=now, last_updated=now, source="published_only")
        except optimiser.ForecastError as exc:
            print(f"Published data unusable: {exc}")
    if snapshot is None:
        return _saved_forecast()
    try:
        _save_forecast(snapshot)
    except OSError as exc:
        print(f"Could not save the live forecast: {exc}")
    return snapshot


def _from_now(snapshot, now):
    """Drop past slots, keeping the one in progress."""
    if snapshot is None:
        return None
    table = snapshot.table[snapshot.table.index >= _slot(now)]
    if table.empty:
        return None
    try:
        optimiser.validate_forecast(table)
    except optimiser.ForecastError as exc:
        print(f"Live forecast rejected: {exc}")
        return None
    return replace(snapshot, table=table)


def current_forecast(now=None):
    """Today's forecast snapshot, or None if live data is off or unavailable."""
    if not live_enabled():
        return None
    now = _utc_now(now)
    return _from_now(_remember("forecast", FORECAST_TTL, now, _refresh_forecast), now)


# --- Grid ----------------------------------------------------------------------


def fetch_grid(now):
    """Latest GB actual carbon intensity and the generation mix for that slot."""
    slot = _slot(now)
    start, end = slot - pd.Timedelta(hours=3), slot + pd.Timedelta(minutes=30)
    carbon = data.fetch_carbon(start.to_pydatetime(), end.to_pydatetime())
    mix = {row["from"]: row for row in data.fetch_generation_mix(start.to_pydatetime(), end.to_pydatetime())}

    for row in sorted(carbon, key=lambda r: r["from"], reverse=True):
        actual = row.get("actual_gco2_kwh")
        shares = mix.get(row["from"])
        if actual is None or shares is None:
            continue
        if actual == 0 and (shares.get("gas_pct") or 0) > 0:
            continue  # Same "suspect zero" rule as the Step 2 dataset
        return {
            "data_as_of": iso(pd.Timestamp(row["from"])),
            "last_updated": iso(now),
            "cached": False,
            "source": "live",
            "historical_demo": False,
            "carbon_intensity": int(actual),
            "mix_percent": {
                fuel: round(float(shares[f"{fuel}_pct"]), 1)
                for fuel in MIX_SOURCES if shares.get(f"{fuel}_pct") is not None
            },
        }
    raise data.DataFetchError("NESO returned no recent actual carbon intensity.")


def _refresh_grid(now):
    try:
        grid = fetch_grid(now)
    except FETCH_ERRORS as exc:
        print(f"Live grid failed, trying the saved copy: {exc}")
        try:
            saved = json.loads((LIVE_DIR / "grid.json").read_text())
        except (OSError, ValueError) as missing:
            print(f"No usable saved live grid: {missing}")
            return None
        # Only a recent reading is worth showing as "live".
        if now - pd.Timestamp(saved["data_as_of"]) > pd.Timedelta(hours=6):
            return None
        return {**saved, "cached": True}
    try:
        LIVE_DIR.mkdir(parents=True, exist_ok=True)
        (LIVE_DIR / "grid.json").write_text(json.dumps(grid, indent=2) + "\n")
    except OSError as exc:
        print(f"Could not save the live grid: {exc}")
    return grid


def current_grid(now=None):
    """Latest grid reading as an API-ready dict, or None if unavailable."""
    if not live_enabled():
        return None
    now = _utc_now(now)
    return _remember("grid", GRID_TTL, now, _refresh_grid)


# --- Command line -------------------------------------------------------------


def check_features(day, features_path=config.DATA_DIR / "features.parquet"):
    """Rebuild a past cutoff's features through the live path and compare with Step 3."""
    cutoff = latest_cutoff(pd.Timestamp(datetime.combine(day, time(23)), tz=config.UK_TIMEZONE).tz_convert("UTC"))
    saved = pd.read_parquet(features_path)
    saved = saved[pd.to_datetime(saved["forecast_at_utc"], utc=True) == cutoff].reset_index(drop=True)
    if saved.empty:
        raise SystemExit(f"{features_path} has no rows for the cutoff {iso(cutoff)}.")
    with tempfile.TemporaryDirectory() as cache_dir:
        rebuilt = live_features(cutoff, cache_dir).reset_index(drop=True)

    print(f"\nCutoff {iso(cutoff)}: {len(rebuilt)} live rows vs {len(saved)} Step 3 rows.")
    worst, mismatched = 0.0, []
    for column in features.FEATURE_COLUMNS:
        a = pd.to_numeric(saved[column], errors="coerce").to_numpy(dtype=float)
        b = pd.to_numeric(rebuilt[column], errors="coerce").to_numpy(dtype=float)
        same_missing = np.isnan(a) == np.isnan(b)
        both = ~np.isnan(a) & ~np.isnan(b)
        diff = float(np.max(np.abs(a[both] - b[both]))) if both.any() else 0.0
        if not same_missing.all() or diff > 1e-6:
            mismatched.append((column, int((~same_missing).sum()), diff))
        worst = max(worst, diff)
    for column, missing, diff in mismatched:
        print(f"  MISMATCH {column}: {missing} rows differ in missingness, max difference {diff:.4g}")
    print(f"{len(features.FEATURE_COLUMNS) - len(mismatched)}/{len(features.FEATURE_COLUMNS)} "
          f"model inputs match exactly (max difference {worst:.3g}).")
    return 1 if mismatched else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--build", action="store_true", help="build the model forecast for the latest cutoff")
    group.add_argument("--check", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(), metavar="YYYY-MM-DD",
                       help="compare live features for that day's 15:00 cutoff with data/features.parquet")
    args = parser.parse_args()
    if args.check:
        return check_features(args.check)

    now = _utc_now()
    cutoff = latest_cutoff(now)
    print(f"Building the model forecast for the {iso(cutoff)} cutoff ...")
    snapshot = build_model_forecast(cutoff, now)
    table = snapshot.table
    upcoming = table[table.index >= _slot(now)]
    print(f"\n{len(table)} slots from {iso(table.index[0])} to {iso(table.index[-1])}; "
          f"{len(upcoming)} still ahead.")
    for source, unit in (("price", "p/kWh"), ("carbon", "g/kWh")):
        width = (upcoming[f"{source}_p90"] - upcoming[f"{source}_p10"]).mean()
        print(f"  {source}: median {upcoming[f'{source}_p50'].min():.1f}-{upcoming[f'{source}_p50'].max():.1f} {unit}, "
              f"mean 80% band width {width:.1f}")
    print(f"Explanations {'ready' if snapshot.contributions else 'unavailable'}. Saved to {LIVE_DIR}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
