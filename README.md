# Grid Advisor

Grid Advisor is a HackTheBurgh 2026 project that will help households choose when to run appliances by showing the trade-off between electricity cost, carbon and convenience. The first region is Edinburgh and South Scotland.

## Run Step 3

With Step 2's `data/dataset.parquet` in place, build the feature table and run the cutoff tests.

```bash
python -m src.features --assume-availability
python -m unittest discover -s tests -v
```

The offline build writes `data/features.parquet`. It uses a 15:00 Europe/London cutoff each day and up to 96 successive half-hour targets starting at that cutoff. Each row contains a UTC cutoff, a UTC target, model inputs, separate price and carbon labels, and availability metadata. Select inputs with `src.features.FEATURE_COLUMNS`. Use `--input`, `--output`, and `--horizon-hours` to change the paths or shorten the horizon.

| Input | How it is built |
| --- | --- |
| Horizon, hour, weekday, month and bank holiday | Horizon is elapsed minutes. Calendar fields use the target's UK local time. The cached Scottish calendar covers 2024–2028. The 2026 World Cup holiday uses its official announcement timestamp. |
| Yesterday and last-week price and carbon | Look up the target's local clock time one or seven days earlier. Keep values only when the earlier slot is available at the cutoff. Repeated autumn times use the second occurrence. Missing spring times stay null. |
| 24h and 7d rolling means | Average available slots in windows ending at the cutoff. A window needs at least 75% of its half-hours present. |
| Weather forecasts | Pick the shortest eligible archived lead for the target and use its wind, gust, cloud, solar radiation and temperature forecasts at four sites. Keep missing fields null. |
| Last-week demand and residual demand proxy | Check Elexon's actual publication timestamp and slot end. Divide last-week demand by 10,000 MW and subtract bounded wind and solar potential scores. The result is a dimensionless indicator. |

Agile lags use the delivery slot's end as a conservative availability bound. Elexon demand uses its recorded publication time. The Step 2 archive lacks exact publication times for GB actual carbon and the GFS runs. The example command opts into an assumed two-hour buffer after carbon slot end and the existing 12-hour GFS release buffer. Recorded times take precedence whenever they are present. For a recorded-only build, omit `--assume-availability`. On the Step 2 snapshot, the carbon history and weather inputs then stay null. ERA5 and archived NESO forecasts are excluded from the model inputs. Flagged carbon actuals are excluded from carbon lags and labels.

The target windows overlap across days. Split by `forecast_at_utc` and use only training labels with an availability time before the first validation cutoff. For price forecasting evaluation, account for which Agile rates were already published when each decision was made.

Calendar source and one-off announcement details are available from [GOV.UK](https://www.gov.uk/bank-holidays) and the [Scottish Government](https://www.gov.scot/news/world-cup-bank-holiday-confirmed/). See [NESO's methodology](https://www.neso.energy/document/373951/download) and the [Open-Meteo Previous Runs API](https://open-meteo.com/en/docs/previous-runs-api) for the data timing conventions.

## Run Step 2

Set up a Python 3.11+ virtual environment from the project folder and install the dependencies.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m src.build_dataset
```

The command downloads 12 months by default. Use `--months 24` for two years or `--start YYYY-MM-DD --end YYYY-MM-DD` to set the UTC window explicitly. The start is inclusive and the end is exclusive. The default end leaves one full day for outturns to arrive. The script prints coverage and basic stats, writes `data/dataset.parquet`, and plots the last two weeks in `data/dataset_sanity.png`.

The pipeline saves up to 28 days per source request in `data/cache/history/`. It reuses those chunks on a rerun. If a request fails, the merge uses cached rows for the requested timestamps wherever available. Remaining gaps stay null. The UTC join keeps a unique row for every half-hour, including the UK’s 46-slot spring settlement day and 50-slot autumn settlement day.

Try the bundled three-day sample without internet. The sample includes GFS forecast history and the matching energy data.

```bash
python -m src.build_dataset --offline --start 2026-11-03 --end 2026-11-06 --output data/sample_dataset.parquet
```

To rebuild from the full Step 2 data bundle, extract it into the project folder and run the same command with `--start 2025-11-06 --end 2026-11-06`. Omit `--output` to write `data/dataset.parquet`.

| Dataset columns | Source and timing |
| --- | --- |
| `time_utc`, `price_p_kwh_inc_vat`, `price_tariff_code` | The UTC slot start, published Octopus Agile rate, and region N tariff active in that historical period. |
| `gb_carbon_actual_gco2_kwh`, `gb_carbon_actual_suspect` | NESO’s GB actual value and a flag for a zero reading in a slot where the GB fuel mix reports gas. |
| `*_carbon_forecast_archive_gco2_kwh`, `gb_*_pct`, `south_scotland_*_pct` | NESO forecasts by valid time and GB and South Scotland fuel shares. The archived carbon forecast rows lack issue timestamps. |
| `demand_mw`, `transmission_demand_mw`, `demand_published_at_utc` | Elexon initial demand outturns and the API’s publication timestamp. |
| `weather_forecast_*_previous_day1/2/3` | NOAA GFS forecasts at 24h, 48h and 72h leads from Open-Meteo Previous Runs, at four sites. Each half-hour uses the value from its UTC hour. |
| `weather_day1/2/3_assumed_available_at_utc` | Assumed availability equals the valid hour minus the archived lead time plus a 12-hour release buffer. These estimates are used to check availability against a decision cutoff. |
| `era5_*` | ERA5 reanalysis for diagnostics. Requests stop seven days before today to allow for archive latency. |

The verified full run contains 17,520 slots from 6 November 2025 through 5 November 2026 UTC. Each has an Agile price, GB actual carbon value and Elexon demand value. One GB actual carbon value was flagged for review. The GFS archive had a 29-hour gap in 72-hour-lead cloud cover. Use ERA5 for historical diagnostics and keep the archived NESO forecasts separate from any forecast correction backtest until the issue times can be established. [Previous Runs API](https://open-meteo.com/en/docs/previous-runs-api)

## Run Step 1

From the project folder with Python 3.11 or newer, run the offline smoke test.

```bash
python3 -m src.check_data --offline
```

It prints a sample from each source and should end with `0 live, 13 cached, 0 unavailable.` The bundled CSVs contain real API responses sampled on 7 November 2026. Their original timestamps are preserved.

To check the live APIs, install Requests in a virtual environment.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install 'requests>=2.32.4,<3'
python -m src.check_data
```

The full dependency list for the project is in `requirements.txt`. Live checks use public endpoints without credentials. Each successful check saves a CSV under `data/cache/`. A failed or empty response uses that check’s latest local CSV, then the bundled sample. The output shows `[LIVE]` or `[CACHED]` and the UTC retrieval time. An unavailable source with no saved sample prints `[UNAVAILABLE]` and causes a nonzero exit. Some live requests timed out during verification and worked on a subsequent call.

## Data checked

The recent sample spans 3–5 November 2026 UTC. The weather archive sample spans 27–29 October to allow for reanalysis latency. Open-Meteo checks use four representative points configured in `config.py`. Weather is hourly, with wind speeds in m/s, temperature in °C, cloud cover in percent and shortwave radiation in W/m².

| Source | Verified live response |
| --- | --- |
| [Octopus Energy](https://developer.octopus.energy/rest/guides/api-basics) | The product list and product detail resolved to `AGILE-24-10-01` and `E-1R-AGILE-24-10-01-N`. The linked rate endpoint returned 144 half-hourly rates in pence per kWh including VAT. The checker discovers the latest domestic Agile import product and uses its monthly direct-debit tariff for region N. |
| [NESO Carbon Intensity API](https://api.carbonintensity.org.uk/) | GB and South Scotland history each returned 144 slots. Their 48-hour forecasts each returned 96 slots. The GB generation mix returned 144 slots. South Scotland is region ID 2. |
| [Elexon Insights](https://bmrs.elexon.co.uk/api-documentation/endpoint) | FUELHH returned 2,880 fuel records over 144 half-hours. Demand outturn and system prices each returned 144 half-hours. Demand uses `/demand/outturn` because the separate actual-total-load endpoint returned 79 of 144 slots for this sample. |
| [Open-Meteo](https://open-meteo.com/en/docs) | Live forecast, historical forecast, ERA5 archive and an ECMWF Single Run each returned 72 hourly rows per site at four sites. The first hour of the Single Run had missing gust and radiation values. |

## Forecast availability

Octopus publishes Agile’s half-hourly rates for the next day in the afternoon or evening, usually near 16:00 local time. Grid Advisor will use published rates whenever available. Price forecasts add value before publication and for later slots. Historical rates give the time each price applies, so a pre-publication backtest also needs a rule for when prices could have been known. [Agile Octopus](https://octopus.energy/smart/agile/)

The NESO GB history includes actual and forecast carbon intensity. The South Scotland history contains forecast intensity and generation mix. The sampled history has no forecast issue timestamp. A correction backtest needs official forecasts with a verifiable issue time. The GB actuals are available as a target. [NESO Carbon Intensity API](https://api.carbonintensity.org.uk/)

Open-Meteo’s historical forecast series stitches early hours from several runs. ERA5 is reanalysis. Use issue-specific weather for a day-ahead backtest, and allow for the delay between a model’s initialisation and when the forecast becomes available. The Single Run sample records its initialisation time in `model_run_at_utc`. [Historical Forecast API](https://open-meteo.com/en/docs/historical-forecast-api) · [Single Runs API](https://open-meteo.com/en/docs/single-runs-api)

The bundled samples are for checking API shape and offline behaviour. Historical training and evaluation need a larger dataset with timestamps for data availability.

Weather data from [Open-Meteo](https://open-meteo.com/) under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), reshaped to one CSV row per site and hour. Other samples come from the public Octopus Energy, NESO and Elexon Insights APIs linked above.
