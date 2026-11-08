"""Step 6: conformal calibration of the Step 5 quantile bands.

The Step 5 models give p10/p50/p90, but their 80% band is too narrow. This
step widens each end separately so that roughly 10% of outcomes fall below
p10 and 10% above p90, which is what the optimiser assumes.

For each target and 12-hour horizon bucket, the lower offset is the
conformal 90% quantile of (p10 - actual) and the upper offset that of
(actual - p90). Calibrated p10 = p10 - lower, p90 = p90 + upper. p50 is
unchanged.

Three versions are reported on the Step 5 test period:
  fixed    offsets from Step 5's validation window (split conformal)
  rolling  for each daily cutoff, offsets from the last 28 days of outcomes
           already published by that cutoff (the primary method, chosen
           before seeing test results because validation coverage was
           already low, which signals drift)
  adaptive delayed-feedback tail budgets on the same rolling score window.
           This revision was evaluated after inspecting the holdout; its
           reported improvement is exploratory, not a fresh validation.

Run:  python -m src.calibration
"""

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

import config
from src import models

LEVEL = 0.8  # Target central coverage
TAIL = (1 - LEVEL) / 2  # 10% allowed below p10 and above p90
BUCKET_HOURS = 12
WINDOW_DAYS = 28
MIN_SCORES = 100  # Below this, rolling falls back to the fixed offsets
ADAPTIVE_RATE = 0.005  # Per newly observed forecast error; exploratory revision.
FLOORS = {"price": None, "carbon": 0.0}  # Carbon intensity cannot be negative


# --- Core conformal pieces (work on one target's long table) -----------------
#
# A long table has one row per forecast and these columns:
#   forecast_at_utc, horizon_minutes, bucket, actual, label_ready_utc, p10, p50, p90


def conformal_quantile(scores, tail=TAIL):
    """The ceil((n + 1)(1 - tail))-th smallest score, the finite-sample conformal rule.

    If that rank exceeds n (very small samples), the largest score is used.
    """
    scores = np.asarray(scores, dtype=float)
    scores = np.sort(scores[np.isfinite(scores)])
    if len(scores) == 0:
        raise ValueError("No finite scores to calibrate on.")
    rank = math.ceil((len(scores) + 1) * (1 - tail))
    return float(scores[min(rank, len(scores)) - 1])


def horizon_bucket(minutes):
    return (np.asarray(minutes, dtype=int) // (BUCKET_HOURS * 60)).astype(int)


def bucket_label(bucket):
    return f"{bucket * BUCKET_HOURS}-{(bucket + 1) * BUCKET_HOURS}h"


def _offsets(rows):
    """(lower, upper, n) widening that puts ~10% of these rows outside each end."""
    rows = rows[np.isfinite(rows["actual"])]
    lower = conformal_quantile(rows["p10"] - rows["actual"])
    upper = conformal_quantile(rows["actual"] - rows["p90"])
    return lower, upper, len(rows)


def fixed_offsets(calibration_rows):
    """{bucket: (lower, upper, n)} from one calibration window."""
    return {int(bucket): _offsets(rows) for bucket, rows in calibration_rows.groupby("bucket")}


def rolling_offsets(pool, cutoff, fallback, window_days=WINDOW_DAYS):
    """Offsets at `cutoff` from outcomes that were published by then.

    Only rows forecast in the `window_days` before the cutoff whose label
    became available at or before the cutoff are used, so nothing from the
    future leaks in. Buckets with fewer than MIN_SCORES outcomes use `fallback`.
    """
    known = pool[
        (pool["label_ready_utc"] <= cutoff)
        & (pool["forecast_at_utc"] < cutoff)
        & (pool["forecast_at_utc"] >= cutoff - pd.Timedelta(days=window_days))
        & np.isfinite(pool["actual"])
    ]
    result = {}
    for bucket in fallback:
        rows = known[known["bucket"] == bucket]
        result[bucket] = _offsets(rows) if len(rows) >= MIN_SCORES else fallback[bucket]
    return result


def apply_offsets(rows, offsets, floor=None):
    """Widen p10 and p90 by the bucket offsets, keeping p10 <= p50 <= p90."""
    lower = rows["bucket"].map(lambda b: offsets[b][0])
    upper = rows["bucket"].map(lambda b: offsets[b][1])
    p10 = np.minimum(rows["p10"] - lower, rows["p50"])
    p90 = np.maximum(rows["p90"] + upper, rows["p50"])
    if floor is not None:
        p10 = np.maximum(p10, floor)
    return rows.assign(p10=p10, p90=p90)


def calibrate_rolling(pool, rows, fallback, floor=None, window_days=WINDOW_DAYS):
    """Apply rolling offsets cutoff by cutoff. Returns calibrated rows and fallback count."""
    parts, fallbacks = [], 0
    for cutoff, group in rows.groupby("forecast_at_utc"):
        offsets = rolling_offsets(pool, cutoff, fallback, window_days)
        fallbacks += sum(offsets[b] is fallback[b] for b in offsets)
        parts.append(apply_offsets(group, offsets, floor))
    return pd.concat(parts).loc[rows.index], fallbacks


# --- Bias-corrected version ------------------------------------------------------
#
# The price model's median runs low when prices shift (in the test period only
# about a quarter of outcomes fall below p50). Widening the tails alone covers
# that with very wide, lopsided bands, and the optimiser's risk penalty then
# rules out every forecast slot. This version first shifts p10/p50/p90 by the
# median recent error, then measures the tails around the shifted forecast.
# Offsets are (shift, lower, upper, n).


def _bias_offsets(rows):
    """Median error, then the tail widening needed around the shifted bands."""
    rows = rows[np.isfinite(rows["actual"])]
    shift = float(np.median(rows["actual"] - rows["p50"]))
    shifted = rows.assign(p10=rows["p10"] + shift, p50=rows["p50"] + shift, p90=rows["p90"] + shift)
    lower, upper, n = _offsets(shifted)
    return shift, lower, upper, n


def fixed_bias_offsets(calibration_rows):
    """{bucket: (shift, lower, upper, n)} from one calibration window."""
    return {int(bucket): _bias_offsets(rows) for bucket, rows in calibration_rows.groupby("bucket")}


def rolling_bias_offsets(pool, cutoff, fallback, window_days=WINDOW_DAYS):
    """Bias offsets at `cutoff`, from outcomes published by then (as rolling_offsets)."""
    known = pool[
        (pool["label_ready_utc"] <= cutoff)
        & (pool["forecast_at_utc"] < cutoff)
        & (pool["forecast_at_utc"] >= cutoff - pd.Timedelta(days=window_days))
        & np.isfinite(pool["actual"])
    ]
    result = {}
    for bucket in fallback:
        rows = known[known["bucket"] == bucket]
        result[bucket] = _bias_offsets(rows) if len(rows) >= MIN_SCORES else fallback[bucket]
    return result


def apply_bias_offsets(rows, offsets, floor=None):
    """Shift all three quantiles, then widen the tails, keeping p10 <= p50 <= p90."""
    shift, lower, upper = (rows["bucket"].map(lambda b, i=i: offsets[b][i]) for i in range(3))
    p50 = rows["p50"] + shift
    p10 = np.minimum(rows["p10"] + shift - lower, p50)
    p90 = np.maximum(rows["p90"] + shift + upper, p50)
    if floor is not None:
        p10, p50, p90 = (np.maximum(q, floor) for q in (p10, p50, p90))
    return rows.assign(p10=p10, p50=p50, p90=p90)


def calibrate_bias(pool, rows, fallback, floor=None, window_days=WINDOW_DAYS):
    """Apply rolling bias offsets cutoff by cutoff. Returns calibrated rows and fallback count."""
    parts, fallbacks = [], 0
    for cutoff, group in rows.groupby("forecast_at_utc"):
        offsets = rolling_bias_offsets(pool, cutoff, fallback, window_days)
        fallbacks += sum(offsets[b] is fallback[b] for b in offsets)
        parts.append(apply_bias_offsets(group, offsets, floor))
    return pd.concat(parts).loc[rows.index], fallbacks


DEPLOYED_BANDS = {"price": "adaptive", "carbon": "rolling"}  # Tail method served live per target


def median_shift_qualifies(report, source):
    """Rule fixed before evaluation: shift the served median only if the deployed
    median is biased (outside 40-60% of outcomes below p50) and the shift brings
    it inside that range on the test period.

    The full bias-corrected method was also tested against coverage >= 75% and
    narrower bands than adaptive. It failed coverage (68%): shifting moves p50
    but measures the same band edges as the rolling version. So only the median
    shift is deployed, on top of the unchanged deployed band edges.
    """
    overall = report[(report["target"] == source) & (report["horizon"] == "all")].set_index("method")
    deployed = overall.loc[DEPLOYED_BANDS[source], "below_p50_pct"]
    shifted = overall.loc["bias_corrected", "below_p50_pct"]
    return bool(not 40 <= deployed <= 60 and 40 <= shifted <= 60)


def with_shifted_median(banded, shifted):
    """Keep one version's band edges and take the other's shifted median, inside the band."""
    p50 = np.clip(shifted["p50"], banded["p10"], banded["p90"])
    return banded.assign(p50=p50)


def calibrate_adaptive(pool, rows, fallback, floor=None, window_days=WINDOW_DAYS,
                       final_cutoff=None):
    """Delayed-feedback tail calibration, inspired by adaptive conformal inference.

    Each tail's error budget starts at 0.1. Newly available past forecast
    outcomes update it by 0.005 * (target misses - observed misses). Each
    outcome is processed once. Budgets are clipped to [0.005, 0.3] for finite
    operational intervals; this modification provides no exact coverage guarantee.
    """
    if not rows.index.is_unique:
        raise ValueError("Adaptive calibration requires unique forecast-row indices.")
    if rows.empty:
        raise ValueError("Adaptive calibration requires forecast rows.")
    if final_cutoff is not None and final_cutoff <= rows.forecast_at_utc.max():
        raise ValueError("Export cutoff must follow all forecast cutoffs.")
    budgets = {bucket: np.array([TAIL, TAIL]) for bucket in fallback}
    emitted, processed, states = [], set(), []
    groups = list(rows.groupby("forecast_at_utc", sort=True))
    if final_cutoff is not None:
        groups.append((final_cutoff, rows.iloc[:0]))
    for cutoff, group in groups:
        if emitted:
            previous = pd.concat(emitted)
            ready = previous[(previous.label_ready_utc <= cutoff)
                             & ~previous.index.isin(processed) & np.isfinite(previous.actual)]
            for bucket, settled in ready.groupby("bucket"):
                misses = np.array([(settled.actual < settled.p10).sum(),
                                   (settled.actual > settled.p90).sum()])
                budgets[bucket] = np.clip(budgets[bucket] + ADAPTIVE_RATE *
                                          (len(settled) * TAIL - misses), 0.005, 0.3)
            processed.update(ready.index)
        known = pool[(pool.label_ready_utc <= cutoff) & (pool.forecast_at_utc < cutoff)
                     & (pool.forecast_at_utc >= cutoff - pd.Timedelta(days=window_days))
                     & np.isfinite(pool.actual)]
        offsets = {}
        for bucket, tail in budgets.items():
            samples = known[known.bucket == bucket]
            offsets[bucket] = fallback[bucket]
            if len(samples) >= MIN_SCORES:
                offsets[bucket] = (
                    conformal_quantile(samples.p10 - samples.actual, tail[0]),
                    conformal_quantile(samples.actual - samples.p90, tail[1]), len(samples))
            states.append({"forecast_at_utc": cutoff, "bucket": bucket,
                           "lower_tail": tail[0], "upper_tail": tail[1],
                           "lower": offsets[bucket][0], "upper": offsets[bucket][1],
                           "n": offsets[bucket][2]})
        if not group.empty:
            emitted.append(apply_offsets(group, offsets, floor))
    return pd.concat(emitted).loc[rows.index], pd.DataFrame(states)


def coverage(rows):
    """Coverage, tail rates, width and pinball loss for rows with an actual."""
    rows = rows[np.isfinite(rows["actual"])]
    y = rows["actual"].to_numpy()
    p10, p50, p90 = (rows[c].to_numpy() for c in ("p10", "p50", "p90"))
    return {
        "rows": len(rows),
        "coverage_80_pct": 100 * np.mean((y >= p10) & (y <= p90)),
        "below_p10_pct": 100 * np.mean(y < p10),
        "below_p50_pct": 100 * np.mean(y < p50),
        "above_p90_pct": 100 * np.mean(y > p90),
        "mean_width": float(np.mean(p90 - p10)),
        "pinball_p10": models.pinball(y, p10, 0.1),
        "pinball_p90": models.pinball(y, p90, 0.9),
    }


# --- Step 5 integration ------------------------------------------------------


def rebuild_predictions(features, model_dir, validation_days=30, test_days=60, purge_hours=52):
    """Predict every post-training row with the saved Step 5 models.

    Uses Step 5's own chronological split, so the models never saw these rows.
    Returns {target: long table} with a `split` column: validation, gap or test.
    """
    train, valid, test = models.chronological_split(features, validation_days, test_days, purge_hours)
    manifest = Path(model_dir) / "training.json"
    legacy = Path(model_dir) / "summary.json"
    if manifest.is_file():
        last_trained = json.loads(manifest.read_text())["last_training_cutoff"]
    elif legacy.is_file():
        last_trained = json.loads(legacy.read_text())["split"]["last_train_cutoff_utc"]
    else:
        raise ValueError("Model training metadata is missing; retrain before calibration.")
    if pd.Timestamp(last_trained) != pd.to_datetime(train.forecast_at_utc, utc=True).max():
        raise ValueError("Saved model training period does not match this split; retrain the models.")
    times = pd.to_datetime(features["forecast_at_utc"], utc=True)
    first_valid = pd.to_datetime(valid["forecast_at_utc"], utc=True).min()
    post = features.loc[times >= first_valid]
    split = pd.Series("gap", index=post.index)
    split[split.index.isin(valid.index)] = "validation"
    split[split.index.isin(test.index)] = "test"

    tables = {}
    for source, (target, _) in models.TARGETS.items():
        predicted = models.predict_quantiles(
            models.load_models(model_dir, source), post, carbon=(source == "carbon")
        )
        table = pd.DataFrame({
            "forecast_at_utc": pd.to_datetime(post["forecast_at_utc"], utc=True),
            "target_at_utc": pd.to_datetime(post["target_at_utc"], utc=True),
            "horizon_minutes": post["horizon_minutes"].astype(int),
            "split": split,
            "actual": pd.to_numeric(post[target], errors="coerce"),
            "label_ready_utc": pd.to_datetime(post[models.LABEL_READY[source]], utc=True),
            "p10": predicted[:, 0],
            "p50": predicted[:, 1],
            "p90": predicted[:, 2],
        }, index=post.index)
        table["bucket"] = horizon_bucket(table["horizon_minutes"])
        tables[source] = table
    return tables


def _check_matches_step5(tables, step5_dir):
    """Confirm the rebuilt test predictions equal Step 5's saved ones."""
    path = Path(step5_dir) / "test_predictions.csv"
    if not path.is_file():
        return None
    saved = pd.read_csv(path)
    worst = 0.0
    for source, table in tables.items():
        test = table[table["split"] == "test"]
        for timestamp in ("forecast_at_utc", "target_at_utc"):
            if not pd.DatetimeIndex(pd.to_datetime(test[timestamp], utc=True)).equals(
                pd.DatetimeIndex(pd.to_datetime(saved[timestamp], utc=True))
            ):
                raise ValueError("Saved predictions have different issue/target timestamps.")
        for q in ("p10", "p50", "p90"):
            worst = max(worst, float(np.max(np.abs(test[q].to_numpy() - saved[f"{source}_{q}"].to_numpy()))))
    return worst


def run(features, *, model_dir=Path("results/step5"), out_dir=Path("results/step6"),
        window_days=WINDOW_DAYS, validation_days=30, test_days=60, purge_hours=52):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tables = rebuild_predictions(features, model_dir, validation_days, test_days, purge_hours)
    mismatch = _check_matches_step5(tables, model_dir)
    if mismatch is not None:
        if not np.isfinite(mismatch) or mismatch > 1e-6:
            raise ValueError("Rebuilt predictions differ from the saved model results.")
        print(f"Rebuilt test predictions vs Step 5 saved: max difference {mismatch:.2e}")

    report, wide, saved_offsets, served_versions = [], None, {}, {}
    for source, table in tables.items():
        floor = FLOORS[source]
        calibration_rows = table[table["split"] == "validation"]
        test = table[table["split"] == "test"]
        fixed = fixed_offsets(calibration_rows)
        versions = {
            "raw": test,
            "fixed": apply_offsets(test, fixed, floor),
        }
        versions["rolling"], fallbacks = calibrate_rolling(table, test, fixed, floor, window_days)
        as_of = table["label_ready_utc"].max()
        export_cutoff = as_of + pd.Timedelta(seconds=1)
        versions["adaptive"], states = calibrate_adaptive(
            table, test, fixed, floor, window_days, final_cutoff=export_cutoff)
        states.to_csv(out_dir / f"{source}_adaptive_state.csv", index=False)
        fixed_bias = fixed_bias_offsets(calibration_rows)
        versions["bias_corrected"], _ = calibrate_bias(table, test, fixed_bias, floor, window_days)
        served_versions[source] = versions
        print(f"{source}: rolling used the fixed fallback in {fallbacks} of "
              f"{test['forecast_at_utc'].nunique() * len(fixed)} cutoff-buckets.")

        for method, rows in versions.items():
            report.append({"target": source, "method": method, "horizon": "all", **coverage(rows)})
            for bucket, part in rows.groupby("bucket"):
                report.append({"target": source, "method": method,
                               "horizon": bucket_label(bucket), **coverage(part)})

        columns = {
            f"{source}_actual": test["actual"],
            f"{source}_p50": test["p50"],
            **{f"{source}_{m}_{q}": versions[m][q] for m in versions for q in ("p10", "p90")},
            f"{source}_bias_corrected_p50": versions["bias_corrected"]["p50"],  # The only shifted median
        }
        block = pd.DataFrame(columns)
        if wide is None:
            wide = test[["forecast_at_utc", "target_at_utc", "horizon_minutes"]].join(block)
        else:
            wide = wide.join(block)

        # Latest rolling offsets, for widening live forecasts in the app.
        latest = rolling_offsets(table, as_of + pd.Timedelta(seconds=1), fixed, window_days)
        bias_latest = rolling_bias_offsets(table, export_cutoff, fixed_bias, window_days)
        adaptive_latest = states[states.forecast_at_utc == export_cutoff]
        saved_offsets[source] = {
            "deployment_method": f"{DEPLOYED_BANDS[source]}_latest",
            "bias_corrected_latest": {bucket_label(b): _bias_dict(v) for b, v in bias_latest.items()},
            "adaptive_latest": {bucket_label(int(row.bucket)): _offset_dict((row.lower, row.upper, row.n))
                                for row in adaptive_latest.itertuples()},
            "rolling_latest": {bucket_label(b): _offset_dict(v) for b, v in latest.items()},
            "fixed_validation": {bucket_label(b): _offset_dict(v) for b, v in fixed.items()},
            "as_of_utc": str(as_of),
        }

    report = pd.DataFrame(report)
    for source in tables:
        if median_shift_qualifies(report, source):
            saved_offsets[source]["median_shift"] = {
                label: values["shift"] for label, values in saved_offsets[source]["bias_corrected_latest"].items()
            }
            # Report what is actually served: deployed band edges plus the shifted median.
            served = with_shifted_median(served_versions[source][DEPLOYED_BANDS[source]],
                                         served_versions[source]["bias_corrected"])
            report = pd.concat([report, pd.DataFrame(
                [{"target": source, "method": "served", "horizon": "all", **coverage(served)}]
                + [{"target": source, "method": "served", "horizon": bucket_label(b), **coverage(part)}
                   for b, part in served.groupby("bucket")])], ignore_index=True)
        print(f"{source} served: {saved_offsets[source]['deployment_method']} bands"
              f"{' + median shift' if 'median_shift' in saved_offsets[source] else ''}")
    report.to_csv(out_dir / "coverage.csv", index=False, float_format="%.4f")
    wide.to_csv(out_dir / "calibrated_test_predictions.csv", index=False)
    (out_dir / "offsets.json").write_text(json.dumps({
        "level": LEVEL,
        "bucket_hours": BUCKET_HOURS,
        "window_days": window_days,
        "deployment_note": "Frozen offsets from historical outcomes; regenerate to update. Adaptive price results are exploratory and do not guarantee 80% live coverage.",
        "usage": "p10 -= lower, p90 += upper for the matching horizon bucket; then, if median_shift "
                 "is present, p50 += shift clipped inside [p10, p90].",
        **saved_offsets,
    }, indent=2) + "\n")
    plot(report, out_dir / "calibration.png")

    print("\nTest-period calibration (all horizons):")
    shown = report[report["horizon"] == "all"].drop(columns="horizon")
    print(shown.to_string(index=False, float_format=lambda v: f"{v:.2f}"))
    print(f"\nSaved coverage.csv, calibrated_test_predictions.csv, offsets.json and calibration.png to {out_dir}")
    return report


def _bias_dict(value):
    shift, lower, upper, n = value
    return {"shift": round(shift, 4), "lower": round(lower, 4), "upper": round(upper, 4), "n": int(n)}


def _offset_dict(value):
    lower, upper, n = value
    return {"lower": round(lower, 4), "upper": round(upper, 4), "n": int(n)}


def plot(report, path):
    """Coverage by horizon, and how often outcomes fall below each quantile."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colours = {"raw": "#9aa0a6", "fixed": "#e8a33d", "rolling": "#2e7d5b", "adaptive": "#4466cc",
               "bias_corrected": "#c0392b"}
    names = {"raw": "Step 5 raw", "fixed": "Fixed (validation window)", "rolling": f"Rolling ({WINDOW_DAYS} days)",
             "adaptive": "Adaptive delayed feedback", "bias_corrected": "Bias-corrected rolling"}
    fig, axes = plt.subplots(2, 2, figsize=(12, 8.5))
    for column, source in enumerate(models.TARGETS):
        part = report[report["target"] == source]
        by_horizon = part[part["horizon"] != "all"]
        horizons = list(dict.fromkeys(by_horizon["horizon"]))
        ax = axes[0, column]
        width = 0.16
        for i, method in enumerate(colours):
            values = by_horizon[by_horizon["method"] == method].set_index("horizon").loc[horizons]
            ax.bar(np.arange(len(horizons)) + (i - (len(colours) - 1) / 2) * width, values["coverage_80_pct"],
                   width, color=colours[method], label=names[method])
        ax.axhline(80, color="#333", linestyle="--", linewidth=1, label="Target 80%")
        ax.set_xticks(np.arange(len(horizons)), horizons)
        ax.set_ylim(0, 100)
        ax.set_title(f"{source.title()}: share inside the 80% band, by hours ahead")
        ax.set_ylabel("% of test outcomes")
        ax.legend(fontsize=8, loc="lower right")

        ax = axes[1, column]
        overall = part[part["horizon"] == "all"].set_index("method")
        nominal = [10, 50, 90]
        ax.plot([0, 100], [0, 100], color="#333", linestyle="--", linewidth=1, label="Perfectly calibrated")
        for method in colours:
            row = overall.loc[method]
            observed = [row["below_p10_pct"], row["below_p50_pct"], 100 - row["above_p90_pct"]]
            ax.plot(nominal, observed, marker="o", color=colours[method], label=names[method])
        ax.set_xticks(nominal, ["p10", "p50", "p90"])
        ax.set_xlim(0, 100)
        ax.set_ylim(0, 100)
        ax.set_title(f"{source.title()}: % of outcomes below each quantile")
        ax.set_ylabel("% of test outcomes below")
        ax.legend(fontsize=8, loc="upper left")
    fig.suptitle("Grid Advisor | Step 6 conformal calibration, test period", fontweight="bold")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--features", type=Path, default=config.DATA_DIR / "features.parquet")
    parser.add_argument("--model-dir", type=Path, default=Path("results/step5"))
    parser.add_argument("--out-dir", type=Path, default=Path("results/step6"))
    parser.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    parser.add_argument("--validation-days", type=int, default=30)
    parser.add_argument("--test-days", type=int, default=60)
    parser.add_argument("--purge-hours", type=int, default=52)
    args = parser.parse_args()
    if not args.features.is_file():
        parser.error(f"Missing {args.features}; run Steps 2 and 3 first.")
    run(models._read_table(args.features), model_dir=args.model_dir, out_dir=args.out_dir,
        window_days=args.window_days, validation_days=args.validation_days,
        test_days=args.test_days, purge_hours=args.purge_hours)


if __name__ == "__main__":
    main()
