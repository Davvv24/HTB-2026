"""Step 5: time-split quantile forecasts for 0–48h Agile price and GB carbon.

Uses ONLY src.features.FEATURE_COLUMNS for the forward-looking models.
Archived NESO forecasts lack issue timestamps; their correction is a *retrospective
scientific diagnostic*, never a verified real-time 48h backtest.
"""

import argparse
import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

import config
from src.build_dataset import CARBON, OFFICIAL
from src.features import FEATURE_COLUMNS

QUANTILES = (0.1, 0.5, 0.9)
TARGETS = {"price": ("label_price_p_kwh", "p/kWh"),
           "carbon": ("label_carbon_gco2_kwh", "gCO2/kWh")}
LABEL_READY = {"price": "price_label_available_at_utc",
               "carbon": "carbon_label_available_at_utc"}


def chronological_split(frame, validation_days=30, test_days=60, purge_hours=52):
    """Daily cutoff split; purge 48h target horizon + 2h carbon release margin.

    Train labels must have been published before the first validation cutoff.
    Validation labels must be published before the first test cutoff.
    """
    if validation_days < 1 or test_days < 1 or purge_hours < 50:
        raise ValueError("Use positive validation/test days and at least a 50h purge gap.")
    cutoffs = pd.DatetimeIndex(pd.to_datetime(frame["forecast_at_utc"], utc=True)).unique().sort_values()
    if len(cutoffs) < validation_days + test_days + 2 * int(np.ceil(purge_hours / 24)) + 10:
        raise ValueError("Not enough forecast cutoffs for a chronologically purged split.")
    first_val = cutoffs[-(validation_days + test_days)]
    first_test = cutoffs[-test_days]
    gap = pd.Timedelta(hours=purge_hours)
    timestamps = pd.to_datetime(frame["forecast_at_utc"], utc=True)
    train = frame.loc[timestamps < first_val - gap].copy()
    valid = frame.loc[(timestamps >= first_val) & (timestamps < first_test - gap)].copy()
    test = frame.loc[timestamps >= first_test].copy()
    if any(x.empty for x in (train, valid, test)):
        raise ValueError("Empty chronological split; choose shorter test/validation periods.")
    for partition, boundary in ((train, first_val), (valid, first_test)):
        # Check both the outcome time and publication/assumed availability.
        for column in LABEL_READY.values():
            if column not in partition:
                raise ValueError(f"Missing {column}: rebuild Step 3 features.")
            published = pd.to_datetime(partition[column], utc=True)
            if (published.dropna() > boundary).any():
                raise ValueError(f"Label {column} not available by next split cutoff: leakage detected.")
    return train, valid, test


def numeric_features(frame):
    """Explicit allowlist prevents accidentally using future targets or metadata."""
    absent = set(FEATURE_COLUMNS) - set(frame.columns)
    if absent:
        raise ValueError(f"Missing Step 3 features: {', '.join(sorted(absent))}")
    x = frame.loc[:, list(FEATURE_COLUMNS)].apply(pd.to_numeric, errors="raise")
    return x.astype("float32")


def fit_quantiles(train, target, estimators=160):
    x = numeric_features(train)
    y = pd.to_numeric(train[target], errors="coerce").to_numpy(dtype=float)
    good = np.isfinite(y)
    if good.sum() < 100:
        raise ValueError(f"Not enough finite training targets for {target}.")
    models = {}
    for q in QUANTILES:
        model = lgb.LGBMRegressor(
            objective="quantile", alpha=q, n_estimators=estimators,
            learning_rate=0.05, num_leaves=23, max_depth=-1,
            min_child_samples=70, subsample=1, colsample_bytree=1,
            reg_lambda=1.0, random_state=42, n_jobs=2, verbosity=-1,
        )
        model.fit(x.loc[good], y[good])
        models[q] = model
    return models


def predict_quantiles(models, frame, *, carbon=False):
    x = numeric_features(frame)
    raw = np.column_stack([models[q].predict(x) for q in QUANTILES])
    # Noncrossing postprocess; models are independently fitted.
    out = np.sort(raw, axis=1)
    if carbon:
        out = np.maximum(0, out)
    return out



def load_models(model_dir, source):
    """Load trained quantiles for inference without retraining the pipeline."""
    if source not in TARGETS:
        raise ValueError(f"Unknown target: {source}")
    model_dir = Path(model_dir)
    # Git on Windows can expand LF to CRLF, invalidating LightGBM's stored
    # tree byte offsets. Universal-newline reading restores the saved format.
    return {q: lgb.Booster(model_str=(model_dir / f"{source}_p{int(q * 100)}.txt").read_text(encoding="utf-8"))
            for q in QUANTILES}


def forecast_from_saved_models(feature_rows, model_dir):
    """A thin output contract for the optimiser/backend: UTC timestamps + p10/p50/p90."""
    required = {"forecast_at_utc", "target_at_utc"}
    if not required.issubset(feature_rows):
        raise ValueError(f"Missing forecast metadata: {sorted(required - set(feature_rows))}")
    output = feature_rows[["forecast_at_utc", "target_at_utc"]].copy().reset_index(drop=True)
    for source in TARGETS:
        predictions = predict_quantiles(load_models(model_dir, source), feature_rows,
                                        carbon=(source == "carbon"))
        for n, label in enumerate(("p10", "p50", "p90")):
            output[f"{source}_{label}"] = predictions[:, n]
    return output


def pinball(y, prediction, q):
    residual = y - prediction
    return float(np.mean(np.maximum(q * residual, (q - 1) * residual)))


def evaluate_quantiles(frame, values, source, split_name):
    target, unit = TARGETS[source]
    actual = pd.to_numeric(frame[target], errors="coerce").to_numpy(dtype=float)
    good = np.isfinite(actual) & np.isfinite(values).all(axis=1)
    if not good.any():
        raise ValueError(f"No usable labels in {split_name}/{source}.")
    y, p = actual[good], values[good]
    error = y - p[:, 1]
    return {
        "split": split_name, "target": source, "unit": unit,
        "rows": int(good.sum()), "mae": float(np.mean(np.abs(error))),
        "rmse": float(np.sqrt(np.mean(error ** 2))),
        "pinball_p10": pinball(y, p[:, 0], .1),
        "pinball_p50": pinball(y, p[:, 1], .5),
        "pinball_p90": pinball(y, p[:, 2], .9),
        "coverage_80_pct": float(100 * np.mean((y >= p[:, 0]) & (y <= p[:, 2]))),
        "mean_interval_width": float(np.mean(p[:, 2] - p[:, 0])),
    }


def baseline_comparisons(frame, predictions, source):
    """Fair paired MAE: model and each baseline evaluated on same test slots.

    The yesterday baseline cannot cover future slots beyond its data cutoff.
    The last-week baseline is a broader-coverage comparator.
    """
    y = pd.to_numeric(frame[TARGETS[source][0]], errors="coerce").to_numpy(dtype=float)
    median = predictions[:, 1]
    rows = []
    for name in ("yesterday", "last_week"):
        key = f"{source}_{name}"
        if key not in frame:
            raise ValueError(f"Missing {key}: expected Step 3 lag features.")
        ref = pd.to_numeric(frame[key], errors="coerce").to_numpy(dtype=float)
        eligible = np.isfinite(y) & np.isfinite(ref) & np.isfinite(median)
        if not eligible.any():
            continue
        for method, values in (("lightgbm_p50", median), (name, ref)):
            err = y[eligible] - values[eligible]
            rows.append({"target": source, "comparator": name, "method": method,
                         "paired_slots": int(eligible.sum()),
                         "coverage_pct": float(100 * eligible.sum() / np.isfinite(y).sum()),
                         "mae": float(np.mean(np.abs(err))),
                         "rmse": float(np.sqrt(np.mean(err ** 2)))})
    return rows


def official_archive_diagnostic(history, frame, train, valid, test, out_dir, estimators=160):
    """Retrospective official-carbon residual fit, NOT issue-time-valid results.

    NESO's historical 'forecast' field is by target valid time, without the
    original publication/issue timestamp; never use this for verified online
    comparisons, model inputs or backtest receipts.
    """
    if OFFICIAL not in history or CARBON not in history:
        return {"status": "skipped", "reason": "Official archive or actual carbon missing."}
    source = history[["time_utc", OFFICIAL, CARBON]].copy()
    source["time_utc"] = pd.to_datetime(source["time_utc"], utc=True)
    source = source.drop_duplicates("time_utc", keep="last").set_index("time_utc")

    def setup(part):
        targets = pd.to_datetime(part["target_at_utc"], utc=True)
        official = source[OFFICIAL].reindex(targets).to_numpy(dtype=float)
        actual = pd.to_numeric(part[TARGETS["carbon"][0]], errors="coerce").to_numpy(dtype=float)
        ready = np.isfinite(official) & np.isfinite(actual)
        return official, actual, ready

    train_off, train_y, train_ok = setup(train)
    test_off, test_y, test_ok = setup(test)
    if train_ok.sum() < 100 or test_ok.sum() == 0:
        return {"status": "skipped", "reason": "Too few archived forecast/actual pairs."}
    # Never uses test labels in fitting. Still NOT a true issue-time forecast.
    x_train = numeric_features(train).loc[train_ok].copy()
    x_train["archived_official_forecast"] = train_off[train_ok]
    correction = lgb.LGBMRegressor(
        objective="regression", n_estimators=estimators, learning_rate=.05,
        num_leaves=17, min_child_samples=80, reg_lambda=2,
        random_state=42, n_jobs=2, verbosity=-1,
    )
    correction.fit(x_train, train_y[train_ok] - train_off[train_ok])
    x_test = numeric_features(test).loc[test_ok].copy()
    x_test["archived_official_forecast"] = test_off[test_ok]
    corrected = np.maximum(0, test_off[test_ok] + correction.predict(x_test))
    base_error = test_y[test_ok] - test_off[test_ok]
    corr_error = test_y[test_ok] - corrected
    correction.booster_.save_model(str(out_dir / "carbon_archive_residual_DIAGNOSTIC_ONLY.txt"))
    return {
        "status": "retrospective_diagnostic_only",
        "warning": "Archive forecast values lack original 15:00 issuance timestamps. Not a prospective 48h comparison; do not feed these values into the app/backtest.",
        "test_rows": int(test_ok.sum()),
        "archive_mae": float(np.mean(np.abs(base_error))),
        "corrected_archive_mae": float(np.mean(np.abs(corr_error))),
        "archive_rmse": float(np.sqrt(np.mean(base_error ** 2))),
        "corrected_archive_rmse": float(np.sqrt(np.mean(corr_error ** 2))),
        "mae_improved": bool(np.mean(np.abs(corr_error)) < np.mean(np.abs(base_error))),
    }


def run(features, history=None, *, out_dir=Path("results/step5"),
        validation_days=30, test_days=60, purge_hours=52, estimators=160):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if "availability_mode" in features:
        modes = features["availability_mode"].dropna().unique().tolist()
        if modes != ["buffered"] and modes != ["recorded"]:
            raise ValueError("Unexpected feature availability mode(s); do not combine protocols.")
    train, valid, test = chronological_split(features, validation_days, test_days, purge_hours)
    train_end = pd.to_datetime(train["forecast_at_utc"], utc=True).max()
    valid_start = pd.to_datetime(valid["forecast_at_utc"], utc=True).min()
    test_start = pd.to_datetime(test["forecast_at_utc"], utc=True).min()
    prediction_table = test[["forecast_at_utc", "target_at_utc", "horizon_minutes"]].copy()
    metrics = []
    paired = []
    for source, (target, _) in TARGETS.items():
        print(f"Training {source} quantiles on {len(train):,} forecast rows ...", flush=True)
        models = fit_quantiles(train, target, estimators)
        for q, model in models.items():
            model.booster_.save_model(str(out_dir / f"{source}_p{int(100*q)}.txt"))
        valid_pred = predict_quantiles(models, valid, carbon=(source == "carbon"))
        test_pred = predict_quantiles(models, test, carbon=(source == "carbon"))
        metrics.append(evaluate_quantiles(valid, valid_pred, source, "validation"))
        metrics.append(evaluate_quantiles(test, test_pred, source, "test"))
        paired.extend(baseline_comparisons(test, test_pred, source))
        prediction_table[f"{source}_actual"] = test[target].to_numpy()
        for j, label in enumerate(("p10", "p50", "p90")):
            prediction_table[f"{source}_{label}"] = test_pred[:, j]

    diagnostic = (official_archive_diagnostic(history, features, train, valid, test, out_dir, estimators)
                  if history is not None else {"status": "skipped", "reason": "Historical dataset not supplied."})
    summary = {
        "split": {
            "train_cutoffs": int(train["forecast_at_utc"].nunique()),
            "validation_cutoffs": int(valid["forecast_at_utc"].nunique()),
            "test_cutoffs": int(test["forecast_at_utc"].nunique()),
            "last_train_cutoff_utc": str(train_end),
            "first_validation_cutoff_utc": str(valid_start),
            "first_test_cutoff_utc": str(test_start),
            "purge_hours": purge_hours,
        },
        "data_limitations": [
            "Step 3 'buffered' weather and carbon availability uses ASSUMED release delays, not measured historical issue timestamps.",
            "NESO historical archived carbon forecasts have no original 15:00 issue timestamps; correction is a retrospective diagnostic only.",
            "The nominal 80% quantile interval is uncalibrated; Step 6 applies conformal calibration.",
        ],
        "official_archive_diagnostic": diagnostic,
    }
    pd.DataFrame(metrics).to_csv(out_dir / "quantile_metrics.csv", index=False, float_format="%.4f")
    pd.DataFrame(paired).to_csv(out_dir / "paired_baselines.csv", index=False, float_format="%.4f")
    prediction_table.to_csv(out_dir / "test_predictions.csv", index=False)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print("\nQuantile results (test):")
    print(pd.DataFrame(metrics).query("split == 'test'").to_string(index=False, float_format=lambda v: f"{v:.2f}"))
    print("\nPaired baseline comparisons:")
    print(pd.DataFrame(paired).to_string(index=False, float_format=lambda v: f"{v:.2f}"))
    print("\nOfficial-carbon archive:", diagnostic)
    print(f"\nSaved models and reports to {out_dir}")
    return pd.DataFrame(metrics), pd.DataFrame(paired), summary


def _read_table(path):
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    if path.suffix.lower() == ".parquet":
        try:
            return pd.read_parquet(path)
        except ImportError as exc:
            raise SystemExit("Install pyarrow to read Parquet: python -m pip install pyarrow") from exc
    raise ValueError("--features/--history must be .parquet or .csv")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, default=config.DATA_DIR / "features.parquet")
    parser.add_argument("--history", type=Path, default=config.DATA_DIR / "dataset.parquet")
    parser.add_argument("--out-dir", type=Path, default=Path("results/step5"))
    parser.add_argument("--validation-days", type=int, default=30)
    parser.add_argument("--test-days", type=int, default=60)
    parser.add_argument("--purge-hours", type=int, default=52)
    parser.add_argument("--estimators", type=int, default=160)
    args = parser.parse_args()
    if not args.features.is_file():
        parser.error(f"Missing {args.features}; run Step 3 first.")
    features = _read_table(args.features)
    history = _read_table(args.history) if args.history.is_file() else None
    run(features, history, out_dir=args.out_dir, validation_days=args.validation_days,
        test_days=args.test_days, purge_hours=args.purge_hours, estimators=args.estimators)


if __name__ == "__main__":
    main()
