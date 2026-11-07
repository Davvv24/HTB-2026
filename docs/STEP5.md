# Step 5 — Price and carbon quantile forecasts

## What is implemented

- LightGBM quantile regressors at **P10, P50 and P90** for Agile electricity prices (p/kWh, VAT included) and **GB** actual carbon intensity (gCO₂/kWh).
- One model per target/quantile, using the 37 **explicitly allowlisted** Step 3 features. Archived official carbon forecasts and future labels are not among the forward-looking inputs.
- Predictions for **all 96 half-hour slots across the next 48 hours** at each **15:00 Europe/London** forecast cutoff.
- Chronological train/validation/test splits, with a **52-hour purge** between partitions so outcomes/published values from one period cannot spill into the next. The 60 most recent daily forecast cutoffs are held out. No random shuffle; no test period tuning.
- Quantile **pinball loss**, P50 **MAE/RMSE**, raw P10–P90 **interval coverage**, and paired comparisons to the Step 3 yesterday/last-week lag baselines.
- A **separate, retrospective-only** LightGBM residual model for `actual carbon - NESO archived forecast`.

## Install & run

The inputs are the existing Step 2/3 files. From the repository root:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_models.py' -v
python -m src.models
```

By default, `python -m src.models` reads `data/features.parquet` and `data/dataset.parquet`, trains six quantile models and the archive-correction diagnostic, then writes results in `results/step5/`.

Options: `--features PATH --history PATH --out-dir PATH --validation-days 30 --test-days 60 --purge-hours 52 --estimators 160`.

If you have CSV files instead of Parquet, supply those paths. The published model output files are plain LightGBM text, portable across machines.

Use the models from Python without retraining:

```python
import pandas as pd
from src.models import forecast_from_saved_models

features = pd.read_parquet('data/features.parquet')
# For a real-time run you would build rows from the information available at NOW,
# rather than using the historical features.parquet rows below.
forecast = forecast_from_saved_models(features.tail(96), 'results/step5')
print(forecast.head())
```

The returned columns are `forecast_at_utc`, `target_at_utc`, `price_p10`, `price_p50`, `price_p90`, `carbon_p10`, `carbon_p50`, and `carbon_p90`. The backend should serve UTC timestamps and let React convert for local display.

## Verified historical results (60-day test)

| Metric | Price | Carbon |
|:--|--:|--:|
| P50 MAE | 8.46 p/kWh | 23.74 gCO₂/kWh |
| P50 RMSE | 10.30 p/kWh | 30.53 gCO₂/kWh |
| P10 pinball loss | 1.84 | 5.15 |
| P50 pinball loss | 4.23 | 11.87 |
| P90 pinball loss | 3.43 | 5.70 |
| **Raw P10–P90 interval coverage** | **34.93%** | **65.00%** |

All 5,760 test rows with targets are included in these model scores.

### Fair paired baseline comparisons

| Target | Comparable slots | Model P50 MAE | Simple baseline MAE |
|:--|--:|--:|--:|
| Price vs yesterday | 2,882 | **7.84** | 7.92 |
| Price vs last week | 5,760 | **8.46** | 11.90 |
| Carbon vs yesterday | 2,642 | **22.94** | 42.04 |
| Carbon vs last week | 5,760 | **23.74** | 57.63 |

The strict yesterday baseline **cannot** forecast the entire 48-hour horizon: its reference reading has not happened at the forecast cutoff. Therefore compare on the *same observed slots* and report its coverage. No results should imply yesterday forecasts all 48 hours.

The raw intervals are **not well-calibrated** (especially for price). Do **not** claim that an 80% prediction interval has 80% empirical coverage until Step 6 conformal calibration is completed. Step 6 must use a dedicated calibration window without touching the test period.

### Archived National Grid forecast correction — diagnostic only

The held-out archived NESO forecasts score **10.96 MAE** against GB actual intensity; the residual-corrected archived series scores **9.28 MAE**.

**Very important caveat:** the archived value is indexed by the *valid target time*, but the source data lacks the original *forecast issue timestamp*. We cannot prove that a forecast value was available at 15:00 when a prediction was made. The corrected result is **not a valid real-time 48-hour comparison** and must **not** be presented as such in the demo, walk-forward backtest, or savings receipts. The residual model is saved as `carbon_archive_residual_DIAGNOSTIC_ONLY.txt` to prevent accidental deployment. To measure genuine outperformance, collect timestamped NESO forecasts at the prediction cutoff going forward and evaluate them on the same delivery slots.

### Data and model limitations

- Step 3's `availability_mode=buffered` relies on **assumed** weather and carbon publication delays when no historical release timestamp is available. This is conservative but not an audited historical issue log. Reconstructed GFS previous-run series may be less faithful than timestamped downloaded issuance snapshots.
- The price forecast matters especially **before Octopus publishes tomorrow's Agile tariff** (normally mid-afternoon). Once the official tariff has been published, use the actual published price instead of pretending to predict it.
- Only **GB** actual carbon is scored here; the regional South Scotland archived forecast is not the truth label.
- Historical forecasts are overlapping (daily 48h windows). Per-slot scores reflect those repeated issue/target predictions, not independent household events.
- Point predictions are trained only before the validation window (not refitted using the holdout). Models are intentionally simple and fixed-hyperparameter for hackathon reliability.

## Outputs

- `results/step5/price_p10.txt`, `price_p50.txt`, `price_p90.txt`
- `results/step5/carbon_p10.txt`, `carbon_p50.txt`, `carbon_p90.txt`
- `results/step5/quantile_metrics.csv`
- `results/step5/paired_baselines.csv`
- `results/step5/test_predictions.csv` (timestamps, labels, and quantiles for the held-out period)
- `results/step5/summary.json` (chronology and diagnostics)
- `results/step5/carbon_archive_residual_DIAGNOSTIC_ONLY.txt` (retrospective only)

Next: **Step 6** will calibrate the interval to ~80% coverage using a separate, chronologically prior calibration window, then verify coverage on the untouched 60-day test period.
