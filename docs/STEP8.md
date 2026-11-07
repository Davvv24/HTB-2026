# Step 8 — Walk-forward receipts

Run from the repository root with the project dependencies installed.

```powershell
python -m pip install -r requirements.txt
python -m src.backtest
python -m unittest discover -s tests -v
```

The default reads the committed `data/features.parquet` and `data/dataset.parquet`. It writes results to `results/step8/`, including separate backtest model files. It requires no live APIs. Override inputs with `--features` and `--dataset`, the destination with `--out-dir`, and the replay length with `--days`.

The default cost weight is zero and risk aversion is 0.5. `--carbon-weight` accepts 0–1 and `--risk-aversion` accepts a non-negative value. Set these before evaluating. Changing settings after inspecting results makes the period exploratory evaluation.

## Replay protocol

The supplied data supports 90 consecutive decisions from 6 August through 3 November 2026, at 15:00 Europe/London. Each decision has a 48-hour forecast. Household jobs finish by the following 07:00, so daily appliance events do not overlap. Battery plans cover the interval until the next daily decision, including the 25-hour autumn clock-change day.

The Step 5 saved models overlap the beginning of this longer holdout. Step 8 therefore fits six separate models with the existing fixed hyperparameters. The last training cutoff is 4 July 2026. Calibration begins on 7 July, with the existing 52-hour purge rules separating partitions. Model weights remain fixed throughout replay. Rolling calibration uses only earlier forecasts whose outcome publication times are at or before each decision. It falls back to the preceding validation-window offsets where necessary. It never applies the final Step 6 offsets retrospectively.

For each decision, the configured washing machine, dishwasher and tumble dryer each run once. Together they consume 5.2 kWh daily. All tasks are available at 15:00 and due next 07:00. These are simulated usage assumptions. EV charging is outside this default household scenario.

Five strategies execute identical tasks and are scored against actual half-hour prices and GB carbon observations.

| Strategy | Schedule |
| --- | --- |
| Run now | Start each appliance at the decision cutoff |
| Fixed overnight | Start each appliance at midnight UK time |
| Yesterday | Optimise using Step 4's same-local-clock-time yesterday forecast, subject to publication availability |
| Model | Optimise with calibrated quantiles and the chosen carbon/risk settings |
| Perfect foresight | Minimise actual cost within the same appliance constraints |

The overnight deadline fits the horizon for which yesterday's observations are known. A day missing required yesterday values or appliance actuals is excluded from every appliance strategy and listed in `summary.json`. The verified run has zero excluded days. Negative prices and negative savings are retained. Perfect foresight supplies a money-saving upper bound. Its emissions are reported, but it does not optimise carbon. The captured-saving percentage uses aggregate model savings divided by aggregate perfect-foresight savings, and is undefined when the denominator is zero.

## Measured results

The default 90-day replay produced these appliance receipts.

| Strategy | Cost GBP | CO2 kg | Saving GBP versus run now | CO2 saving kg versus run now |
| --- | ---: | ---: | ---: | ---: |
| Run now | 138.91 | 51.658 | 0.00 | 0.000 |
| Fixed overnight | 116.78 | 54.467 | 22.12 | -2.809 |
| Yesterday | 115.00 | 52.529 | 23.91 | -0.871 |
| Model | 109.03 | 51.122 | 29.88 | 0.536 |
| Perfect foresight | 99.86 | 49.511 | 39.04 | 2.148 |

The model captures **76.53%** of the perfect-foresight money saving.

The battery is a separate household scenario using the existing synthetic load profile and battery parameters in `config.py`. Initial charge is 5 kWh. Each daily plan must finish at least as full as it started, and the realised final state carries into the next day. Costs include efficiency losses and degradation. The replay rejects simultaneous charging/discharging from the LP and missing actuals rather than publishing those battery receipts.

Battery net savings versus the same load without a battery were **GBP 57.11**, with **GBP 0.00** maximum drawdown of cumulative daily savings. The annualised daily-savings Sharpe was **28.06**, calculated as `sqrt(365) * mean(daily GBP savings) / sample_std(daily GBP savings)`. This describes this simulated savings series. It is not a return on invested capital, and correlated daily observations limit its interpretation. Initial zero cumulative savings is included when measuring drawdown. Capital cost and export revenue are excluded, and terminal stored energy receives no cash credit.

## Outputs and integration

- `summary.csv` contains strategy totals and captured-saving percentages.
- `summary.json` contains chronology, settings, coverage, exclusions, battery metrics and assumptions.
- `appliance_receipts.csv` records every appliance event, scheduled start/end, energy, actual cost and actual emissions.
- `daily_savings.csv` supplies daily and cumulative values for the receipts chart.
- `battery_daily.csv` and `battery_schedule.csv` supply battery P&L, charge continuity and half-hour energy flows.
- `forecasts.csv` preserves the calibrated forecasts used by the replay.
- `cumulative_savings.png` plots appliance and battery savings.
- `models/` contains the six models trained specifically for this holdout.

The later FastAPI receipts endpoint can serve these precomputed files. `src.backtest.run()` also returns the summary table and metadata.

## Interpretation and checks

Historical weather and carbon publication availability follows the existing buffered assumptions. It is not an audited archive of what was available at every decision. Future Agile prices are treated as unknown because historical publication timestamps are unavailable, even where rates might actually have been published. These receipts describe that forecast-only protocol. GB carbon is the scoring target. The retrospective official-carbon correction is excluded.

The full suite passed 67 tests when this step was implemented. New tests cover realised scoring and unit conversion, negative prices, missing observations, future-label exclusion in calibration, equal task energy, the perfect-foresight cost bound, the autumn clock change, initial-loss drawdown, and battery charge/accounting. Newer NumPy versions can emit deprecation warnings from existing pandas timestamp operations while these tests pass.
