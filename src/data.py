"""Small clients for Grid Advisor's public data sources."""

from datetime import UTC, date, datetime, timedelta

import config


class DataFetchError(RuntimeError):
    """A data source could not provide the requested records."""


def _get_json(url, params=None):
    try:
        import requests  # The offline check runs without third-party packages.
    except ModuleNotFoundError as exc:
        raise DataFetchError("Requests is unavailable. Install it with pip.") from exc

    try:
        response = requests.get(url, params=params, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise DataFetchError(f"{url}: {exc}") from exc


def _utc_minute(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")


def _in_window(rows, time_key, start, end):
    """Use a half-open interval even when the upstream API overlaps its edges."""
    return [
        row for row in rows
        if start <= datetime.fromisoformat(row[time_key]) < end
    ]


def _octopus_pages(url, params=None):
    while url:
        page = _get_json(url, params)
        yield from page["results"]
        url = page["next"]
        params = None  # Next-page links already contain the query.


def discover_agile_product(at: datetime) -> dict:
    """Find the newest available domestic Agile import product."""
    products = []
    for product in _octopus_pages(f"{config.OCTOPUS_URL}/products/"):
        if (
            product["code"].startswith("AGILE-")
            and product["direction"] == "IMPORT"
            and not product["is_business"]
            and not product["is_prepay"]
            and datetime.fromisoformat(product["available_from"]) <= at
            and (
                product["available_to"] is None
                or at < datetime.fromisoformat(product["available_to"])
            )
        ):
            products.append(product)
    if not products:
        raise DataFetchError("No active domestic Agile import product found.")

    return max(products, key=lambda p: datetime.fromisoformat(p["available_from"]))


def fetch_agile_rates(start: datetime, end: datetime, at: datetime) -> list[dict]:
    """Get published region N unit rates, in pence per kWh including VAT."""
    product = discover_agile_product(at)
    product_code = product["code"]
    detail = _get_json(f"{config.OCTOPUS_URL}/products/{product_code}/")
    tariffs = detail["single_register_electricity_tariffs"]
    tariff = tariffs[f"_{config.AGILE_REGION}"]["direct_debit_monthly"]
    rates_url = next(
        (link["href"] for link in tariff["links"] if link["rel"] == "standard_unit_rates"),
        None,
    )
    if rates_url is None:
        raise DataFetchError(f"No unit-rate link found for {tariff['code']}.")

    rows = _octopus_pages(rates_url, {
        "period_from": _utc_minute(start),
        "period_to": _utc_minute(end),
        "page_size": 250,
    })
    return [
        {
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "price_p_kwh_inc_vat": row["value_inc_vat"],
            "product_code": product_code,
            "tariff_code": tariff["code"],
        }
        for row in _in_window(rows, "valid_from", start, end)
    ]


def _mix_columns(row):
    return {f"{fuel['fuel']}_pct": fuel["perc"] for fuel in row["generationmix"]}


def fetch_carbon(start: datetime, end: datetime | None = None, *, region_id=None) -> list[dict]:
    """Fetch a history window or, with no end supplied, the 48-hour forecast."""
    stop = _utc_minute(end) if end is not None else "fw48h"
    path = f"/intensity/{_utc_minute(start)}/{stop}"
    if region_id is not None:
        path = f"/regional{path}/regionid/{region_id}"

    data = _get_json(f"{config.CARBON_URL}{path}")["data"]
    if region_id is not None:
        region = data[0] if isinstance(data, list) else data
        data = region["data"]

    rows = []
    for row in _in_window(data, "from", start, end or start + timedelta(hours=48)):
        record = {
            "from": row["from"],
            "to": row["to"],
            "forecast_gco2_kwh": row["intensity"]["forecast"],
            "actual_gco2_kwh": row["intensity"].get("actual"),
        }
        if region_id is not None:
            record.update(region_id=region["regionid"], region=region["shortname"])
            record.update(_mix_columns(row))
        rows.append(record)
    return rows


def fetch_generation_mix(start: datetime, end: datetime) -> list[dict]:
    """Get the GB generation mix in percentages."""
    url = f"{config.CARBON_URL}/generation/{_utc_minute(start)}/{_utc_minute(end)}"
    data = _get_json(url)["data"]
    return [
        {"from": row["from"], "to": row["to"], **_mix_columns(row)}
        for row in _in_window(data, "from", start, end)
    ]


def _elexon_settlement_range(endpoint, first, last):
    return _get_json(f"{config.ELEXON_URL}/{endpoint}", {
        "settlementDateFrom": first.isoformat(),
        "settlementDateTo": last.isoformat(),
        "format": "json",
    })["data"]


def fetch_elexon_generation(first: date, last: date) -> list[dict]:
    """Get half-hourly generation by fuel (MW) for inclusive UK settlement dates."""
    return _elexon_settlement_range("datasets/FUELHH", first, last)


def fetch_elexon_demand(first: date, last: date) -> list[dict]:
    """Get GB initial demand and transmission-system demand outturn (MW)."""
    return _elexon_settlement_range("demand/outturn", first, last)


def fetch_elexon_system_prices(first: date, last: date) -> list[dict]:
    """Get imbalance prices (£/MWh) for inclusive UK settlement dates."""
    rows = []
    for offset in range((last - first).days + 1):
        day = (first + timedelta(days=offset)).isoformat()
        url = f"{config.ELEXON_URL}/balancing/settlement/system-prices/{day}"
        rows.extend(_get_json(url, {"format": "json"})["data"])
    keys = ("startTime", "settlementDate", "settlementPeriod", "createdDateTime",
            "systemBuyPrice", "systemSellPrice", "netImbalanceVolume")
    return [{key: row[key] for key in keys} for row in rows]


def fetch_weather(source: str, **window) -> list[dict]:
    """Fetch hourly weather at the four sample points, with wind in m/s."""
    params = {
        "latitude": ",".join(str(lat) for _, lat, _ in config.WEATHER_POINTS),
        "longitude": ",".join(str(lon) for _, _, lon in config.WEATHER_POINTS),
        "hourly": ",".join(config.WEATHER_FIELDS),
        "timezone": "GMT",
        "cell_selection": "nearest",
        "wind_speed_unit": "ms",
        **window,
    }
    data = _get_json(config.WEATHER_URLS[source], params)
    rows = []
    for point, location in zip(config.WEATHER_POINTS, data, strict=True):
        hourly = location["hourly"]
        for index, time in enumerate(hourly["time"]):
            row = {
                "location": point[0],
                "time_utc": f"{time}Z",
                **{field: hourly[field][index] for field in config.WEATHER_FIELDS},
            }
            if "run" in window:
                row["model_run_at_utc"] = f"{window['run']}Z"
            rows.append(row)
    return rows
