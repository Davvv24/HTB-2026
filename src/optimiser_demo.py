"""Show the optimiser's input table built from today's published data.

Run with `python -m src.optimiser_demo`. Add `--offline` to skip the APIs.
"""

import argparse
from datetime import UTC, datetime, timedelta

import config
from src import data, optimiser
from src.check_data import load_sample


def load_live(now):
    """Published Agile prices and the NESO 48-hour GB carbon forecast from now."""
    slot = now.replace(minute=now.minute // 30 * 30, second=0, microsecond=0)
    agile = data.fetch_agile_rates(slot, slot + timedelta(hours=48), now)
    carbon = data.fetch_carbon(slot)
    return optimiser.make_forecast_from_published(agile, carbon)


def load_cached():
    """Saved Agile prices and NESO GB history for the same past window."""
    agile, agile_path = load_sample("octopus_agile")
    carbon, carbon_path = load_sample("neso_gb_history")
    if not agile or not carbon:
        raise optimiser.ForecastError("No saved Agile or carbon data to fall back on.")
    retrieved = agile[0]["retrieved_at_utc"]
    print(f"Using saved data from {agile_path.parent.name}/, retrieved {retrieved}.")
    return optimiser.make_forecast_from_published(agile, carbon)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Use only saved data.")
    args = parser.parse_args()

    forecast = None
    if not args.offline:
        try:
            forecast = load_live(datetime.now(UTC))
            print("[LIVE] Published Agile prices and the NESO GB carbon forecast.")
        except (data.DataFetchError, optimiser.ForecastError, KeyError) as exc:
            print(f"Live data failed — {exc}")
    if forecast is None:
        forecast = load_cached()
        print("[CACHED] These slots are in the past, so they only demonstrate the format.")

    london = forecast.tz_convert(config.UK_TIMEZONE)
    first, last = london.index[0], london.index[-1] + optimiser.SLOT
    print(f"\n{len(forecast)} half-hour slots, {first:%a %d %b %H:%M} to {last:%a %d %b %H:%M} London time.")
    print(f"Published prices: {int(forecast.price_published.sum())} of {len(forecast)} slots.")

    view = london[["price_p50", "carbon_p50"]].rename(
        columns={"price_p50": "price p/kWh", "carbon_p50": "carbon g/kWh"}
    )
    view.index = view.index.strftime("%a %H:%M")
    print("\nFirst and last six slots (London time):")
    print(view.head(6).to_string())
    print("...")
    print(view.tail(6).to_string())

    for column, unit in (("price_p50", "p/kWh"), ("carbon_p50", "g/kWh")):
        low, high = london[column].idxmin(), london[column].idxmax()
        print(
            f"\n{column.split('_')[0].title()}: lowest {london[column].min():g} {unit} at {low:%a %H:%M}, "
            f"highest {london[column].max():g} {unit} at {high:%a %H:%M}."
        )

    show_appliance_plans(forecast)
    return 0


def _local(when):
    return when.tz_convert(config.UK_TIMEZONE).strftime("%a %H:%M")


def show_appliance_plans(forecast):
    """Recommend a start for each preset, from the first slot to the end of the table."""
    earliest = forecast.index[0]
    deadline = forecast.index[-1] + optimiser.SLOT
    print(f"\nRecommendations for runs between {_local(earliest)} and {_local(deadline)} (London time):")
    for key in config.APPLIANCES:
        appliance = optimiser.get_appliance(key)
        print(f"\n{appliance.name} ({appliance.power_kw:g} kW for {appliance.duration_slots / 2:g} h)")
        for label, weight in (("Cheapest", 0.0), ("Balanced", 0.5), ("Greenest", 1.0)):
            plan = optimiser.plan_appliance(forecast, appliance, earliest, deadline, carbon_weight=weight)
            print(
                f"  {label:<9} start {_local(plan.start_utc)} | {plan.cost_p:5.1f}p, {plan.co2_kg:.2f} kg CO2"
                f" | vs run now: saves {plan.saving_p:5.1f}p and {round(plan.saving_co2_kg, 2) + 0:+.2f} kg CO2"
            )
        print(f"  Run now   start {_local(earliest)} | {plan.run_now_cost_p:5.1f}p, {plan.run_now_co2_kg:.2f} kg CO2")


if __name__ == "__main__":
    raise SystemExit(main())
