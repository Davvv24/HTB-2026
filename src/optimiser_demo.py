"""Try the Step 7 optimiser on today's published prices.

    python -m src.optimiser_demo                  # overview of every appliance
    python -m src.optimiser_demo --appliance dishwasher --finish-by 07:00
    python -m src.optimiser_demo --appliance ev --ev-kwh 20 --carbon-weight 1
    python -m src.optimiser_demo --appliance battery --json

Add --offline to use saved data instead of the live APIs.
"""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta

import pandas as pd

import config
from src import data, optimiser
from src.check_data import load_sample


def log(message):
    """Status lines go to stderr so --json output stays clean."""
    print(message, file=sys.stderr)


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
    log(f"Using saved data from {agile_path.parent.name}/, retrieved {retrieved}.")
    return optimiser.make_forecast_from_published(agile, carbon)


def load_forecast(offline):
    """Live published data, falling back to the saved sample if anything fails."""
    if not offline:
        try:
            forecast = load_live(datetime.now(UTC))
            log("[LIVE] Published Agile prices and the NESO GB carbon forecast.")
            return forecast
        except (data.DataFetchError, optimiser.ForecastError, KeyError) as exc:
            log(f"Live data failed — {exc}")
    forecast = load_cached()
    log("[CACHED] These slots are in the past, so they only demonstrate the format.")
    return forecast


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--offline", action="store_true", help="use only saved data")
    parser.add_argument("--appliance", choices=[*config.APPLIANCES, "ev", "battery"],
                        help="recommend for one appliance instead of the overview")
    parser.add_argument("--finish-by", metavar="HH:MM",
                        help="London-time deadline, the next time it comes round (default: end of data)")
    parser.add_argument("--carbon-weight", type=float, default=0.5,
                        help="0 = cheapest, 1 = greenest (default 0.5)")
    parser.add_argument("--risk", type=float, default=0.5,
                        help="risk aversion, 0 = trust the median forecast (default 0.5)")
    parser.add_argument("--ev-kwh", type=float, default=30.0, help="energy the EV needs (default 30)")
    parser.add_argument("--json", action="store_true", help="print the plan as API-ready JSON")
    args = parser.parse_args()

    forecast = load_forecast(args.offline)
    if args.appliance:
        return recommend(forecast, args)
    show_overview(forecast)
    show_appliance_plans(forecast)
    show_ev_plan(forecast)
    show_battery_plan(forecast)
    return 0


def _next_local(after, hhmm):
    """The next London-time HH:MM strictly after `after`, as UTC."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    local = after.tz_convert(config.UK_TIMEZONE).to_pydatetime()
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local:
        # Step a day then reset the clock time, so it stays HH:MM across clock changes.
        target = (target + timedelta(days=1)).replace(hour=hour, minute=minute)
    return pd.Timestamp(target).tz_convert("UTC")


def recommend(forecast, args):
    """Print one recommendation, like the app's recommendation card."""
    earliest = forecast.index[0]
    end_of_data = forecast.index[-1] + optimiser.SLOT
    deadline = _next_local(earliest, args.finish_by) if args.finish_by else end_of_data
    weights = {"carbon_weight": args.carbon_weight, "risk_aversion": args.risk}
    try:
        if args.appliance == "battery":
            plan = optimiser.plan_battery(forecast, **weights)
        elif args.appliance == "ev":
            plan = optimiser.plan_ev(forecast, args.ev_kwh, earliest, deadline, **weights)
        else:
            appliance = optimiser.get_appliance(args.appliance)
            plan = optimiser.plan_appliance(forecast, appliance, earliest, deadline, **weights)
    except (optimiser.InfeasibleError, ValueError) as exc:
        log(f"Not possible: {exc}")
        return 1

    if args.json:
        print(json.dumps(plan.to_dict(), indent=2))
        return 0

    if args.appliance == "battery":
        s = plan.schedule
        print(f"\nBattery: {plan.next_action} now.")
        print(f"  Charge {_periods(s['charge_kwh'] > 1e-6)}")
        print(f"  Use battery {_periods(s['discharge_kwh'] > 1e-6)}")
        versus = "no battery"
    elif args.appliance == "ev":
        blocks = ", ".join(_span(start, end) for start, end in plan.blocks())
        print(f"\nCharge your EV ({args.ev_kwh:g} kWh) during {blocks}.")
        versus = "charging now"
    else:
        finish = plan.end_utc.tz_convert(config.UK_TIMEZONE)
        print(f"\nRun your {plan.appliance.lower()} at {_local(plan.start_utc)} (finishes {finish:%H:%M}).")
        for alt in plan.alternatives:
            print(f"  Also good: {_local(alt.start_utc)}, {_versus_now(alt.saving_p / 100, 'GBP', 2)}")
        versus = "running now"
    print(f"  Compared with {versus}: {_versus_now(plan.saving_p / 100, 'GBP', 2)}, "
          f"{_versus_now(plan.saving_co2_kg, 'kg CO2', 2)}.")
    print(f"  {plan.to_dict()['why']}")
    if plan.uses_forecast:
        print("  Uses forecast prices that Octopus has not published yet.")
    return 0


def show_overview(forecast):
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


def _periods(active):
    """Turn a boolean series of slots into 'Sun 01:30-04:00' style ranges."""
    ranges, start = [], None
    for slot, on in active.items():
        if on and start is None:
            start = slot
        if not on and start is not None:
            ranges.append((start, slot))
            start = None
    if start is not None:
        ranges.append((start, active.index[-1] + optimiser.SLOT))
    return ", ".join(_span(a, b) for a, b in ranges) or "none"


def _span(start, end):
    """'Sat 19:30-22:00', or 'Sat 19:30-Sun 09:00' when it runs past midnight."""
    same_day = (start.tz_convert(config.UK_TIMEZONE).date()
                == (end - optimiser.SLOT).tz_convert(config.UK_TIMEZONE).date())
    finish = end.tz_convert(config.UK_TIMEZONE).strftime("%H:%M") if same_day else _local(end)
    return f"{_local(start)}-{finish}"


def show_battery_plan(forecast):
    """Plan a home battery over the whole forecast with the default settings."""
    b = config.BATTERY
    print(
        f"\nHome battery: {b['capacity_kwh']:g} kWh, {b['power_kw']:g} kW, "
        f"{b['round_trip_eff']:.0%} round trip, {b['degradation_p_per_kwh']:g}p/kWh wear, "
        f"starts with {b['initial_kwh']:g} kWh. Covers home use only (no export)."
    )
    for label, weight in (("Cheapest", 0.0), ("Greenest", 1.0)):
        plan = optimiser.plan_battery(forecast, carbon_weight=weight)
        s = plan.schedule
        pad = " " * len(label)
        print(f"  {label}: charge {_periods(s['charge_kwh'] > 1e-6)}")
        print(f"  {pad}  use battery {_periods(s['discharge_kwh'] > 1e-6)}")
        print(
            f"  {pad}  household {s['load_kwh'].sum():.1f} kWh: "
            f"{plan.cost_without_p / 100:.2f} GBP without battery, "
            f"{(plan.cost_with_p + plan.degradation_p) / 100:.2f} GBP with it "
            f"(incl. {plan.degradation_p / 100:.2f} wear)"
        )
        print(
            f"  {pad}  {_versus_now(plan.saving_p / 100, 'GBP', 2)}, "
            f"{_versus_now(plan.saving_co2_kg, 'kg', 2)} | do now: {plan.next_action}"
        )


def _versus_now(saving, unit, places):
    """Word a saving against running now, e.g. 'saves 2.92 GBP' or 'costs 0.02 GBP more'."""
    saving = round(saving, places) + 0
    if saving == 0:
        return f"same {unit} as now"
    word = "saves" if saving > 0 else "costs"
    return f"{word} {abs(saving):.{places}f} {unit}{'' if saving > 0 else ' more'}"


def show_ev_plan(forecast, energy_kwh=30.0):
    """Charge an EV by 07:00 the next morning, London time, if the data reaches it."""
    earliest = forecast.index[0]
    deadline = min(_next_local(earliest, "07:00"), forecast.index[-1] + optimiser.SLOT)
    print(f"\nEV charging: {energy_kwh:g} kWh at {config.EV_CHARGER_KW:g} kW by {_local(deadline)}")
    try:
        for label, weight in (("Cheapest", 0.0), ("Greenest", 1.0)):
            plan = optimiser.plan_ev(forecast, energy_kwh, earliest, deadline, carbon_weight=weight)
            blocks = ", ".join(_span(start, end) for start, end in plan.blocks())
            print(
                f"  {label:<9} {blocks} | {plan.cost_p / 100:.2f} GBP, {plan.co2_kg:.1f} kg CO2"
                f" | {_versus_now(plan.saving_p / 100, 'GBP', 2)}, {_versus_now(plan.saving_co2_kg, 'kg', 1)}"
            )
        print(f"  Run now   from {_local(earliest)} | {plan.run_now_cost_p / 100:.2f} GBP, "
              f"{plan.run_now_co2_kg:.1f} kg CO2")
    except optimiser.InfeasibleError as exc:
        print(f"  Not possible: {exc}")


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
