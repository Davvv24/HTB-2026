# Current model and product assumptions

Updated 8 November 2026. This describes the code and committed artifacts, not guarantees about household savings. Configuration lives in [config.py](../config.py).

## Data and timing

- Prices are Octopus Agile region N (South Scotland), in pence/kWh including VAT. Standing charges are excluded from comparisons; they do not change with the schedule. No export tariff is modelled.
- Carbon labels and realised emissions use GB average grid intensity, not marginal emissions or a household-specific supply mix. South Scotland is also available in the source feeds, but is not the trained carbon target.
- The committed historical dataset covers 6 November 2025 through 5 November 2026. Its latest complete 48-hour forecast starts at 15:00 UK on 3 November. That date is the historical replay cutoff, not a limit on live data.
- Forecast examples are issued daily at 15:00 Europe/London and contain 96 half-hour targets (48 elapsed hours). Storage and joins use UTC; calendar features and user choices use UK local time.
- Historical availability is partly assumed: carbon uses recorded availability where present, otherwise slot end plus two hours; historical price lags are conservatively available after slot end. Elexon observations require their publication time and slot end to have passed.
- Archived GFS weather uses 24/48/72-hour leads and a 12-hour release buffer. The freshest eligible lead is chosen. These are availability assumptions, not audited publication logs. ERA5 reanalysis is not a model input.
- Weather is sampled at South Scotland, North Sea, East Anglia and South England; hourly values repeat into half-hour slots. These four sites are proxies for GB conditions, not a physical grid simulation.
- Missing inputs remain missing; the pipeline does not invent interpolated target observations. Suspect zero carbon readings with positive gas generation are excluded. Duplicate timestamps are resolved by the dataset pipeline.
- Historical replay price overlays assume a 16:00 UK publication rule for a 23:00 boundary. Exact historical publication timestamps are unavailable. Live overlays instead use rates actually returned by Octopus.

## Features and fitted models

- Only the 37 columns in `src.features.FEATURE_COLUMNS` are model inputs. They include horizon, local calendar, available yesterday/last-week values, rolling means, weather and demand proxies. Labels and future actuals are excluded.
- The Scottish holiday calendar covers 2024–2028; one-off holidays use their announcement time. Yesterday/week lookups use local clock time, select the second repeated autumn hour and leave missing spring hours null.
- Rolling 24-hour and seven-day means require 75% of slots. Wind potential uses a clipped cubic 3–12 m/s proxy across four sites; solar uses land-site radiation scaled by 1,000 W/m². Residual demand is a dimensionless proxy using last-week demand divided by 10,000 MW minus wind/solar potential.
- Six separate LightGBM quantile models predict price and carbon p10/p50/p90. Defaults: 160 trees, learning rate 0.05, 23 leaves, minimum 70 child samples, L2 regularisation 1, seed 42, two worker threads, all rows/features per tree.
- Quantiles are sorted per row to avoid crossing. Carbon is floored at zero; negative prices remain valid. The p50 is a median, not a guaranteed most likely value or expected mean.
- Models are frozen after fitting. Chronological splits use a 52-hour purge and 30-day validation allocation. Step 5/6 evaluate a 60-day test period; Step 8 fits separate models before a 90-day holdout. These are different model families/windows and their metrics must not be mixed.

## Uncertainty calibration and deployment

- Fixed calibration uses validation errors. Rolling calibration uses only outcomes available at the forecast cutoff, from the previous 28 days, separately for each 12-hour horizon bucket. Buckets with fewer than 100 scores fall back to validation offsets.
- Each tail uses finite-sample-ranked residual scores. Very small samples cap the requested rank at the largest available score. Widening preserves the median and quantile order, and the carbon floor.
- Adaptive calibration starts each tail budget at 0.1. Once a previous emitted interval's actual becomes available, its miss updates the budget by `0.005 * (0.1 - miss)`. Updates are batched at daily cutoffs, counted once per forecast row, and clipped to [0.005, 0.3]. Overlapping forecasts of the same actual remain separate forecast rows.
- The method is inspired by [Adaptive Conformal Inference Under Distribution Shift](https://arxiv.org/abs/2106.00170), but clipping, delayed feedback and batch updates mean the original theoretical guarantees must not be claimed for this implementation.
- On the existing 60-day Step 5 evaluation, price coverage improves from **68.44% rolling to 76.37% adaptive**, while mean interval width increases from **19.72 to 23.66 p/kWh**. Carbon retains rolling calibration (**81.49%** coverage). Price still falls below the nominal 80% target.
- This revision followed inspection of the existing holdout. These are exploratory results, not independent validation. Fresh future evaluation is needed. Slotwise coverage is not simultaneous 48-hour coverage or a confidence bound on total schedule cost.
- Live inference uses Step 5 models with the exported `adaptive_latest` price offsets and `rolling_latest` carbon offsets in `results/step6/offsets.json`. These are **frozen snapshots** of historical calibration, not continuous online feedback. Regenerate Step 6 with matching data/models and restart the API to update them. Saved live model forecasts are invalidated when this file changes.
- The committed Step 8 replay and its receipts still use their original rolling calibration and separate Step 8 models. Their results do not measure the newly deployed adaptive price bands. The historical app fallback uses those Step 8 forecasts.
- Published prices collapse the price interval to the known rate. Carbon uncertainty remains separate. Neither larger risk aversion nor an 80% nominal band guarantees a saving.

## Scheduling

- Appliance presets assume constant average power: washer 0.5 kW for two hours (1 kWh), dishwasher 0.6 kW for two hours (1.2 kWh), dryer 2 kW for 1.5 hours (3 kWh). These are examples, not measured appliance profiles.
- Each appliance runs contiguously inside the requested window, starting on a half-hour boundary. The earliest requested time rounds up. There is no shared household power limit or coordination among independently requested plans.
- Risk adjustment uses `median + risk_aversion * (p90 - median)`. Appliance/EV objectives normalise candidate price and carbon scores separately; carbon weight 0 means money only and 1 means carbon only. Ties prefer earlier schedules. Normalisation means weight is not a carbon price.
- Reported cost and carbon estimates use median forecasts. They are not full probabilistic expectations. Comparisons use the same requested job starting at its earliest feasible time.
- EV charging assumes 7 kW, defaults to 30 kWh and may be interrupted across slots, with a partial final slot. No vehicle-specific efficiency, initial SOC or charging curve is included.
- The battery optimiser assumes 10 kWh capacity, 5 kW power, 90% round-trip efficiency (split symmetrically between charge/discharge), 2p wear per kWh charged or discharged, and initially 5 kWh. Terminal SOC must be at least initial SOC.
- Battery load follows a synthetic hourly UK profile of about 8.6 kWh/day. No export, capital costs, separate terminal salvage credit or simultaneous charge/discharge is allowed; a MILP handles cases where the LP would exploit simultaneous cycling. The battery API/UI planner is not currently exposed on main; battery receipts are simulated offline results.

## Backtest, receipts and savings goals

- The committed replay uses 90 daily decisions, 6 August–3 November 2026, with training ending 4 July and calibration starting 7 July. All 90 days have matched appliance and battery evaluations.
- Each configured appliance runs once daily, available at 15:00 UK and due next 07:00. Fixed overnight starts at midnight. EV charging is not part of these receipts.
- Comparisons are immediate start, fixed overnight, yesterday forecast, model and perfect foresight. The UI currently shows four strategies, omitting yesterday. The oracle minimises actual money cost under the same constraints; it is not a carbon-saving bound.
- Future prices are treated as unknown in Step 8 because historical publication times are absent. The app's historical price overlay and live published-price path are therefore different protocols from the receipts.
- Battery replay is a separate household; its savings must not be added to appliance receipts as if both were one measured home. SOC carries between daily plans, including 23/25-hour DST days. Its baseline is the same synthetic household without a battery.
- Committed rolling-calibration receipts show £29.88 appliance savings, 76.53% of the oracle's saving and £57.11 battery savings after wear. These are simulations, not customer bills or live adaptive-model results.
- Battery Sharpe annualises daily GBP savings with sqrt(365), not investment returns. Daily serial dependence limits interpretation. Drawdown includes an initial zero balance.
- My Savings uses the latest 30 simulated days, with example goals of £25 for the appliance household and £20 for the battery household. Streaks count non-loss days; green points reward positive daily savings/carbon reductions. It is not linked to a user account or meter.

## Live operation and explanations

- Live builds reuse the feature pipeline around the latest 15:00 UK cutoff. The app can serve a model forecast with published rates, published Agile/NESO data alone, cached usable data, or the labelled historical replay.
- Forecast cache TTL is ten minutes, grid TTL five minutes, failed refresh retry one minute. The background loop runs every five minutes. Cached grid fallback accepts readings up to six hours old. Provider success and model readiness are independent; the horizon can be shorter than 48 hours.
- Cold requests return the available fallback while refresh runs in the background. An exhausted saved model cannot mask fresh published data. `GRID_ADVISOR_LIVE=0` selects offline operation.
- Detailed explanations use native LightGBM feature contributions, consistent with sorted quantiles and the carbon floor. They describe model associations, not causal effects or a decomposition of the risk-adjusted optimiser objective. Published-price attribution is omitted where model attribution is inapplicable.
- If detailed contributions are unavailable, the API's plain-English `why` sentence is displayed. Cached forecasts and backtest artifacts require honest freshness/simulation labels; historical results do not establish current real-world savings.

Implementation references: [features](../src/features.py), [models](../src/models.py), [calibration](../src/calibration.py), [optimiser](../src/optimiser.py), [backtest](../src/backtest.py), [live](../src/live.py), [API](../app/main.py).
