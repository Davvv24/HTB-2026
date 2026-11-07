# Grid Advisor

Grid Advisor will help households choose when to run appliances by showing the trade-off between electricity cost, carbon and convenience. The first milestone is a data check for Edinburgh and South Scotland.

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
