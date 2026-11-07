# Step 4: Baseline forecasting

The two reference methods for **Agile price (p/kWh, including VAT)** and **GB actual carbon intensity (gCO2/kWh)** are:

- **Yesterday:** use the observed value for the same UK-local clock time one calendar day earlier.
- **Last week:** use the observed value for the same UK-local clock time seven calendar days earlier.

These are deliberately simple forecasting methods, not learned models. Each prediction is made at **15:00 Europe/London**, and only includes references known at that point. The default test uses the **60 most recent daily cutoffs** with complete 48-hour target windows. The target rows overlap between days by design, because they are different forecast instances.

## Run

```bash
python -m pip install -r requirements.txt
python -m src.baselines
```

By default the command reads `data/dataset.parquet` from Step 2 and writes `data/baseline_metrics.csv`. You can use `--input path/to/dataset.csv` for an equivalent CSV and vary `--holdout-days 60` or `--horizon-hours 24`. The script refuses an input without a separate preceding history period. Run the tests with:

```bash
python -m unittest discover -s tests -p 'test_baselines.py' -v
```

## Evaluation and leakage rules

- **MAE** is the average absolute prediction error; **RMSE** is the square root of mean squared prediction error. Lower is better. Units are **p/kWh** and **gCO2/kWh**, respectively.
- Both baselines are **scored on the identical intersection** of target slots for which both references and the actual target are present. The `common_slots` column indicates this sample size. This is a fair like-for-like comparison.
- `coverage_pct` and `available_slots` refer to the independent fraction/count of eligible actual labels for which each baseline is available, not model interval coverage.
- A strict yesterday reference becomes unavailable for part of a 48-hour prediction window. Using the observed future reading would leak information. Report its coverage; **do not replace missing predictions with future values**. For Step 5 comparisons, either use the same common-slot subset or evaluate both on a shorter horizon with declared coverage.
- Agile values are conservatively treated as available **after their delivery slot**, even though day-ahead rates may be published earlier. This avoids relying on unavailable historical publication timestamps.
- Carbon actuals are treated as known at their recorded publication timestamp if supplied by the data, or else **assumed** known two hours after the slot ends. The two-hour assumption is not a verified historical publication log; it must be disclosed.
- The repeated autumn clock hour maps to the second occurrence of the corresponding historical hour; missing spring clock times remain unavailable.
- Suspect zero-carbon observations identified in Step 2 are excluded from labels and references.

## What Step 5 should consume

`make_predictions(dataset, cutoffs, horizon_hours=48)` returns `forecast_at_utc`, `target_at_utc`, `horizon_minutes`, `actual_price`, `actual_carbon`, and `price_*/carbon_*` columns. Train/test periods must be chronological; the holdout data should not be used to tune the ML model. For model-to-baseline comparisons, restrict to predictions with observed labels and equal coverage/horizons.
