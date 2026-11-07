"""Shared settings for the Grid Advisor project."""

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
SAMPLES_DIR = DATA_DIR / "samples"

AGILE_REGION = "N"  # Edinburgh / South Scotland
CARBON_REGION_ID = 2  # NESO South Scotland
UK_TIMEZONE = "Europe/London"
CHECK_DAYS = 3
HTTP_TIMEOUT = (10, 15)  # Connect and read timeouts, in seconds

OCTOPUS_URL = "https://api.octopus.energy/v1"
CARBON_URL = "https://api.carbonintensity.org.uk"
ELEXON_URL = "https://data.elexon.co.uk/bmrs/api/v1"
WEATHER_URLS = {
    "forecast": "https://api.open-meteo.com/v1/forecast",
    "historical_forecast": "https://historical-forecast-api.open-meteo.com/v1/forecast",
    "archive": "https://archive-api.open-meteo.com/v1/archive",
    "single_run": "https://single-runs-api.open-meteo.com/v1/forecast",
}

WEATHER_POINTS = (
    ("South Scotland", 55.7, -4.2),
    ("North Sea", 56.0, 1.8),
    ("East Anglia", 52.5, 0.8),
    ("South England", 51.2, -1.5),
)
WEATHER_FIELDS = (
    "wind_speed_100m",
    "wind_gusts_10m",
    "cloud_cover",
    "shortwave_radiation",
    "temperature_2m",
)

# Step 7 appliance presets: (display name, average kW while running, half-hour slots).
# Typical values, chosen as assumptions. Energy per run = kW x slots x 0.5 h.
APPLIANCES = {
    "washing_machine": ("Washing machine", 0.5, 4),  # 1.0 kWh over 2 h
    "dishwasher": ("Dishwasher", 0.6, 4),  # 1.2 kWh over 2 h
    "tumble_dryer": ("Tumble dryer", 2.0, 3),  # 3.0 kWh over 1.5 h
}
EV_CHARGER_KW = 7.0  # Typical home wallbox

# Step 7 home battery defaults. Assumptions: a typical 10 kWh home battery.
BATTERY = {
    "capacity_kwh": 10.0,
    "power_kw": 5.0,  # Max charge or discharge rate
    "round_trip_eff": 0.9,  # Energy out / energy in
    "degradation_p_per_kwh": 2.0,  # Wear cost per kWh charged or discharged
    "initial_kwh": 5.0,
}
# Average household draw (kW) for each London-time hour, about 8.6 kWh a day.
# A simple assumed profile: low overnight, morning bump, evening peak.
HOUSEHOLD_LOAD_KW_BY_HOUR = (
    0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.3, 0.5, 0.5, 0.25, 0.25, 0.25,
    0.25, 0.25, 0.25, 0.25, 0.5, 0.7, 0.7, 0.7, 0.7, 0.4, 0.4, 0.25,
)
