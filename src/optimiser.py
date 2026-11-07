"""Turn half-hourly price and carbon forecasts into appliance schedules.

Every function here works on a *forecast table*: a DataFrame indexed by
`time_utc` (UTC half-hour slot starts, no gaps) with these columns.

    price_p10, price_p50, price_p90     p/kWh including VAT
    carbon_p10, carbon_p50, carbon_p90  gCO2/kWh
    price_published                     True when the price is the real Agile rate

Step 5 and 6 models will produce this table. Until then,
`make_forecast_from_published` builds it from published prices and the
official carbon forecast, with zero-width bands.
"""

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import linprog

import config

SLOT = pd.Timedelta(minutes=30)
SLOT_HOURS = 0.5

PRICE_BAND = ("price_p10", "price_p50", "price_p90")
CARBON_BAND = ("carbon_p10", "carbon_p50", "carbon_p90")
FORECAST_COLUMNS = (*PRICE_BAND, *CARBON_BAND, "price_published")


class ForecastError(ValueError):
    """The forecast table breaks the agreed format."""


def _series(rows, time_key, value_key):
    """Turn API or CSV rows into a float series indexed by UTC slot start."""
    frame = pd.DataFrame(rows, columns=[time_key, value_key])
    times = pd.to_datetime(frame[time_key], utc=True, format="ISO8601")
    values = pd.to_numeric(frame[value_key], errors="coerce")
    series = pd.Series(values.to_numpy(), index=times).dropna()
    return series[~series.index.duplicated(keep="last")].sort_index()


def _first_contiguous_run(index):
    """Keep slots up to the first gap so the table never skips a half-hour."""
    gaps = index.to_series().diff().gt(SLOT).to_numpy()
    return index[: gaps.argmax()] if gaps.any() else index


def make_forecast_from_published(agile_rows, carbon_rows):
    """Build a forecast table from published Agile rates and NESO carbon rows.

    Prices are real, so their band has zero width and `price_published` is True.
    Carbon uses the official NESO forecast as the median, also with zero width,
    until calibrated carbon bands are available. Only slots that have both a
    price and a carbon value are kept, cut at the first gap.
    """
    price = _series(agile_rows, "valid_from", "price_p_kwh_inc_vat")
    carbon = _series(carbon_rows, "from", "forecast_gco2_kwh")
    index = _first_contiguous_run(price.index.intersection(carbon.index))

    table = pd.DataFrame(index=pd.DatetimeIndex(index, name="time_utc"))
    for column in PRICE_BAND:
        table[column] = price.reindex(index).to_numpy()
    for column in CARBON_BAND:
        table[column] = carbon.reindex(index).to_numpy()
    table["price_published"] = True
    return validate_forecast(table)


def validate_forecast(forecast):
    """Raise ForecastError unless the table matches the agreed format."""
    if forecast.empty:
        raise ForecastError("The forecast table has no slots.")

    missing = [column for column in FORECAST_COLUMNS if column not in forecast]
    if missing:
        raise ForecastError(f"Missing columns: {', '.join(missing)}.")

    index = forecast.index
    if not isinstance(index, pd.DatetimeIndex) or str(index.tz) != "UTC":
        raise ForecastError("The index must be UTC timestamps.")
    if not index.is_monotonic_increasing or index.has_duplicates:
        raise ForecastError("Slots must be sorted and unique.")
    if (index != index.floor(SLOT)).any():
        raise ForecastError("Slots must start on the hour or half-hour.")
    if len(index) > 1 and (index.to_series().diff().iloc[1:] != SLOT).any():
        raise ForecastError("Slots must be consecutive half-hours with no gaps.")

    values = forecast[[*PRICE_BAND, *CARBON_BAND]]
    if values.isna().any().any():
        raise ForecastError("Price and carbon bands must have no missing values.")
    for low, mid, high in (PRICE_BAND, CARBON_BAND):
        if ((forecast[low] > forecast[mid]) | (forecast[mid] > forecast[high])).any():
            raise ForecastError(f"Expected {low} <= {mid} <= {high} in every slot.")

    published = forecast["price_published"].astype(bool)
    if (forecast.loc[published, "price_p10"] != forecast.loc[published, "price_p90"]).any():
        raise ForecastError("Published prices must have a zero-width band.")
    return forecast


# --- Appliance scheduling -------------------------------------------------


class InfeasibleError(ValueError):
    """No schedule fits between the earliest start and the deadline."""


@dataclass(frozen=True)
class Appliance:
    name: str
    power_kw: float  # Average draw while running
    duration_slots: int  # Half-hours it must run without stopping

    @property
    def energy_per_slot_kwh(self):
        return self.power_kw * SLOT_HOURS


def get_appliance(key):
    """Look up a preset from config.APPLIANCES, such as 'washing_machine'."""
    return Appliance(*config.APPLIANCES[key])


@dataclass
class Plan:
    appliance: str
    slots: list  # UTC slot starts the appliance runs in
    cost_p: float  # Expected cost using median prices
    co2_kg: float  # Expected emissions using median carbon intensity
    run_now_cost_p: float
    run_now_co2_kg: float
    uses_forecast: bool  # True if any slot's price is a forecast, not published
    alternatives: list = field(default_factory=list)  # Next best non-overlapping plans
    slot_energy_kwh: list = field(default_factory=list)  # Energy used in each of `slots`
    run_now_start_utc: pd.Timestamp | None = None

    def blocks(self):
        """Group the slots into continuous (start, end) runs, in UTC."""
        runs = []
        for slot in self.slots:
            if runs and runs[-1][1] == slot:
                runs[-1][1] = slot + SLOT
            else:
                runs.append([slot, slot + SLOT])
        return [tuple(run) for run in runs]

    @property
    def start_utc(self):
        return self.slots[0]

    @property
    def end_utc(self):
        return self.slots[-1] + SLOT

    @property
    def saving_p(self):
        """Money saved against running now. Negative means it costs more."""
        return self.run_now_cost_p - self.cost_p

    @property
    def saving_co2_kg(self):
        return self.run_now_co2_kg - self.co2_kg

    def to_dict(self):
        """JSON-ready summary in the /api/recommend shape. Times are UTC ISO strings."""
        from src.explain import explain_plan

        return {
            "why": explain_plan(self),
            "appliance": self.appliance,
            "start": _iso(self.start_utc),
            "end": _iso(self.end_utc),
            "blocks": [{"start": _iso(a), "end": _iso(b)} for a, b in self.blocks()],
            "energy_kwh": round(sum(self.slot_energy_kwh), 3),
            "cost_gbp": round(self.cost_p / 100, 2),
            "co2_kg": round(self.co2_kg, 3),
            "run_now": {
                "start": _iso(self.run_now_start_utc),
                "cost_gbp": round(self.run_now_cost_p / 100, 2),
                "co2_kg": round(self.run_now_co2_kg, 3),
            },
            "saving_gbp": round(self.saving_p / 100, 2),
            "saving_co2_kg": round(self.saving_co2_kg, 3),
            "uses_forecast": self.uses_forecast,
            "alternatives": [alt.to_dict() for alt in self.alternatives],
        }


def _iso(when):
    return None if when is None else when.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc(when, name):
    when = pd.Timestamp(when)
    if when.tzinfo is None:
        raise ValueError(f"{name} must include a time zone, such as UTC.")
    return when.tz_convert("UTC")


def _scale(values):
    """Rescale to 0 (best) to 1 (worst). All-equal values score 0."""
    spread = values.max() - values.min()
    return (values - values.min()) / spread if spread > 0 else values * 0.0


def plan_appliance(forecast, appliance, earliest_utc, deadline_utc,
                   carbon_weight=0.0, risk_aversion=0.0, alternatives=2):
    """Pick the best uninterrupted window for an appliance.

    carbon_weight: 0 picks the cheapest window, 1 the greenest, values between
        blend the two after rescaling each to 0-1 across the feasible windows.
    risk_aversion: 0 plans on the median forecast. 1 plans on the 90th
        percentile, so windows that might turn out expensive or dirty lose out.
        Published prices have no band, so they are never penalised.
    Ties go to the earliest start, so a flat forecast recommends running now.
    """
    validate_forecast(forecast)
    if not 0 <= carbon_weight <= 1:
        raise ValueError("carbon_weight must be between 0 and 1.")
    if risk_aversion < 0:
        raise ValueError("risk_aversion cannot be negative.")

    earliest = _utc(earliest_utc, "earliest_utc").ceil(SLOT)
    deadline = _utc(deadline_utc, "deadline_utc")
    slots = appliance.duration_slots
    energy = appliance.energy_per_slot_kwh

    # Sum each column over every run of `slots` consecutive half-hours,
    # labelled by the run's first slot.
    windows = forecast[[*PRICE_BAND, *CARBON_BAND]].rolling(slots).sum().shift(1 - slots)
    windows["published"] = (
        forecast["price_published"].astype(int).rolling(slots).sum().shift(1 - slots) == slots
    )
    starts = windows.index
    windows = windows[
        windows["price_p50"].notna()
        & (starts >= earliest)
        & (starts + slots * SLOT <= deadline)
    ]
    if windows.empty:
        raise InfeasibleError(_infeasible_reason(forecast, appliance, earliest, deadline))

    cost = windows["price_p50"] * energy
    co2 = windows["carbon_p50"] * energy / 1000
    cautious_cost = cost + risk_aversion * (windows["price_p90"] - windows["price_p50"]) * energy
    cautious_co2 = co2 + risk_aversion * (windows["carbon_p90"] - windows["carbon_p50"]) * energy / 1000
    score = (1 - carbon_weight) * _scale(cautious_cost) + carbon_weight * _scale(cautious_co2)

    run_now = windows.index[0]
    ranked = score.sort_values(kind="stable").index  # Stable keeps earlier starts first on ties.

    def make_plan(start):
        return Plan(
            appliance=appliance.name,
            slots=list(pd.date_range(start, periods=slots, freq=SLOT)),
            cost_p=float(cost[start]),
            co2_kg=float(co2[start]),
            run_now_cost_p=float(cost[run_now]),
            run_now_co2_kg=float(co2[run_now]),
            uses_forecast=not bool(windows.at[start, "published"]),
            slot_energy_kwh=[energy] * slots,
            run_now_start_utc=run_now,
        )

    best = make_plan(ranked[0])
    taken = [best]
    for start in ranked[1:]:
        if len(taken) > alternatives:
            break
        if all(abs(start - plan.start_utc) >= slots * SLOT for plan in taken):
            taken.append(make_plan(start))
    best.alternatives = taken[1:]
    return best


def _infeasible_reason(forecast, appliance, earliest, deadline):
    needed = appliance.duration_slots * SLOT
    covered_until = forecast.index[-1] + SLOT
    if earliest >= covered_until:
        return f"The forecast only runs until {covered_until:%d %b %H:%M} UTC."
    available = min(deadline, covered_until) - max(earliest, forecast.index[0])
    hours = max(available.total_seconds(), 0) / 3600
    return (
        f"{appliance.name} needs {needed.total_seconds() / 3600:g} h, "
        f"but only {hours:g} h is available before the deadline."
    )


# --- EV charging -------------------------------------------------------------


def plan_ev(forecast, energy_kwh, earliest_utc, deadline_utc, power_kw=None,
            carbon_weight=0.0, risk_aversion=0.0):
    """Choose the best half-hours to deliver `energy_kwh` before the deadline.

    Charging can pause, so the slots need not be consecutive. Each slot is
    scored per kWh with the same cost/carbon blend and risk rule as
    plan_appliance. The best slots charge at full power, and the last one
    chosen only tops up what is left. "Run now" charges at full power from
    the earliest slot until done.
    """
    validate_forecast(forecast)
    power_kw = config.EV_CHARGER_KW if power_kw is None else power_kw
    if energy_kwh <= 0 or power_kw <= 0:
        raise ValueError("energy_kwh and power_kw must be positive.")
    if not 0 <= carbon_weight <= 1:
        raise ValueError("carbon_weight must be between 0 and 1.")
    if risk_aversion < 0:
        raise ValueError("risk_aversion cannot be negative.")

    earliest = _utc(earliest_utc, "earliest_utc").ceil(SLOT)
    deadline = _utc(deadline_utc, "deadline_utc")
    per_slot = power_kw * SLOT_HOURS
    needed = math.ceil(round(energy_kwh / per_slot, 9))
    name = f"EV charging ({energy_kwh:g} kWh)"

    starts = forecast.index
    usable = forecast[(starts >= earliest) & (starts + SLOT <= deadline)]
    if len(usable) < needed:
        charger = Appliance(name, power_kw, needed)
        raise InfeasibleError(_infeasible_reason(forecast, charger, earliest, deadline))

    price = usable["price_p50"]
    carbon = usable["carbon_p50"]
    cautious_price = price + risk_aversion * (usable["price_p90"] - price)
    cautious_carbon = carbon + risk_aversion * (usable["carbon_p90"] - carbon)
    score = (1 - carbon_weight) * _scale(cautious_price) + carbon_weight * _scale(cautious_carbon)

    def energy_split(slots):
        """Full power in each slot, with the remainder in the last one."""
        split = [per_slot] * (len(slots) - 1)
        return split + [energy_kwh - per_slot * (len(slots) - 1)]

    chosen = list(score.sort_values(kind="stable").index[:needed])  # Best first
    chosen_energy = dict(zip(chosen, energy_split(chosen)))
    run_now = list(usable.index[:needed])
    run_now_energy = dict(zip(run_now, energy_split(run_now)))

    def totals(energy_by_slot):
        cost = sum(price[slot] * kwh for slot, kwh in energy_by_slot.items())
        co2 = sum(carbon[slot] * kwh for slot, kwh in energy_by_slot.items()) / 1000
        return float(cost), float(co2)

    cost, co2 = totals(chosen_energy)
    now_cost, now_co2 = totals(run_now_energy)
    slots = sorted(chosen)
    return Plan(
        appliance=name,
        slots=slots,
        cost_p=cost,
        co2_kg=co2,
        run_now_cost_p=now_cost,
        run_now_co2_kg=now_co2,
        uses_forecast=not bool(usable.loc[slots, "price_published"].all()),
        slot_energy_kwh=[chosen_energy[slot] for slot in slots],
        run_now_start_utc=run_now[0],
    )


# --- Home battery --------------------------------------------------------------


@dataclass
class BatteryPlan:
    schedule: pd.DataFrame  # Per slot: charge, discharge, stored, load and grid kWh
    cost_without_p: float  # Household load bought from the grid, no battery
    cost_with_p: float  # Grid purchases with the battery, before wear
    degradation_p: float  # Wear cost of the planned cycling
    co2_without_kg: float
    co2_with_kg: float
    uses_forecast: bool

    @property
    def saving_p(self):
        """Net saving after battery wear. Zero when cycling is not worth it."""
        return self.cost_without_p - self.cost_with_p - self.degradation_p

    @property
    def saving_co2_kg(self):
        return self.co2_without_kg - self.co2_with_kg

    @property
    def next_action(self):
        """What to do in the first slot: 'charge', 'discharge' or 'hold'."""
        first = self.schedule.iloc[0]
        if first["charge_kwh"] > 1e-6:
            return "charge"
        return "discharge" if first["discharge_kwh"] > 1e-6 else "hold"

    def to_dict(self):
        """JSON-ready summary with the per-slot schedule. Times are UTC ISO strings."""
        from src.explain import explain_battery

        return {
            "why": explain_battery(self),
            "next_action": self.next_action,
            "cost_without_gbp": round(self.cost_without_p / 100, 2),
            "cost_with_gbp": round(self.cost_with_p / 100, 2),
            "degradation_gbp": round(self.degradation_p / 100, 2),
            "saving_gbp": round(self.saving_p / 100, 2),
            "saving_co2_kg": round(self.saving_co2_kg, 3),
            "uses_forecast": self.uses_forecast,
            "schedule": [
                {"time": _iso(time), **{key: round(float(value), 3) for key, value in row.items()}}
                for time, row in self.schedule.iterrows()
            ],
        }


def typical_load(index):
    """Assumed household use in kWh per slot from config.HOUSEHOLD_LOAD_KW_BY_HOUR."""
    hours = index.tz_convert(config.UK_TIMEZONE).hour
    kw = [config.HOUSEHOLD_LOAD_KW_BY_HOUR[hour] for hour in hours]
    return pd.Series(kw, index=index, dtype=float) * SLOT_HOURS


def plan_battery(forecast, load_kwh=None, carbon_weight=0.0, risk_aversion=0.0, **battery):
    """Plan charge and discharge for every slot with a linear programme.

    Variables per slot t: c_t energy drawn from the grid into the battery,
    d_t energy the battery supplies to the home, s_t energy stored after t.

        minimise  sum  buy_t * c_t - sell_t * d_t + wear * (c_t + d_t)
        subject to
            s_t = s_(t-1) + c_t * eta - d_t / eta    eta = sqrt(round trip)
            0 <= s_t <= capacity
            0 <= c_t <= power * 0.5 h
            0 <= d_t <= min(power * 0.5 h, load_t)   no export: it only covers home use
            s_last >= initial                        no free energy from draining it

    buy_t and sell_t are the slot's value per kWh. With carbon_weight > 0,
    carbon is mapped onto the price range and blended in. Risk aversion
    plans charging on the upper band and discharging on the lower band, so
    uncertain spreads are discounted. To re-plan as forecasts update, call
    again with initial_kwh set to the current stored energy and act on the
    first slot only.
    """
    validate_forecast(forecast)
    settings = {**config.BATTERY, **battery}
    capacity = settings["capacity_kwh"]
    step = settings["power_kw"] * SLOT_HOURS
    eta = math.sqrt(settings["round_trip_eff"])
    wear = settings["degradation_p_per_kwh"]
    initial = settings["initial_kwh"]
    if not 0 < settings["round_trip_eff"] <= 1:
        raise ValueError("round_trip_eff must be in (0, 1].")
    if capacity <= 0 or step <= 0 or wear < 0 or not 0 <= initial <= capacity:
        raise ValueError("Check battery capacity, power, wear cost and initial charge.")
    if not 0 <= carbon_weight <= 1 or risk_aversion < 0:
        raise ValueError("carbon_weight must be 0-1 and risk_aversion non-negative.")

    load = typical_load(forecast.index) if load_kwh is None else load_kwh.reindex(forecast.index)
    if load.isna().any() or (load < 0).any():
        raise ValueError("load_kwh needs a non-negative value for every forecast slot.")

    buy, sell = _battery_values(forecast, carbon_weight, risk_aversion)
    slots = len(forecast)
    charge, discharge, stored = range(0, slots), range(slots, 2 * slots), range(2 * slots, 3 * slots)

    cost = np.zeros(3 * slots)
    cost[list(charge)] = buy + wear
    cost[list(discharge)] = -sell + wear

    balance = np.zeros((slots, 3 * slots))
    for t in range(slots):
        balance[t, stored[t]] = 1
        balance[t, charge[t]] = -eta
        balance[t, discharge[t]] = 1 / eta
        if t > 0:
            balance[t, stored[t - 1]] = -1
    balance_rhs = np.zeros(slots)
    balance_rhs[0] = initial

    keep_charge = np.zeros((1, 3 * slots))
    keep_charge[0, stored[-1]] = -1

    bounds = (
        [(0, step)] * slots
        + [(0, min(step, value)) for value in load]
        + [(0, capacity)] * slots
    )
    result = linprog(cost, A_ub=keep_charge, b_ub=[-initial], A_eq=balance, b_eq=balance_rhs,
                     bounds=bounds, method="highs")
    if result.status != 0:
        raise RuntimeError(f"Battery optimisation failed: {result.message}")

    x = np.clip(result.x, 0, None).round(6)
    schedule = pd.DataFrame({
        "charge_kwh": x[list(charge)],
        "discharge_kwh": x[list(discharge)],
        "stored_kwh": x[list(stored)],
        "load_kwh": load.to_numpy(),
    }, index=forecast.index)
    schedule["grid_kwh"] = schedule["load_kwh"] + schedule["charge_kwh"] - schedule["discharge_kwh"]

    price, carbon = forecast["price_p50"], forecast["carbon_p50"]
    active = (schedule["charge_kwh"] > 1e-6) | (schedule["discharge_kwh"] > 1e-6)
    return BatteryPlan(
        schedule=schedule,
        cost_without_p=float((schedule["load_kwh"] * price).sum()),
        cost_with_p=float((schedule["grid_kwh"] * price).sum()),
        degradation_p=float(wear * (schedule["charge_kwh"] + schedule["discharge_kwh"]).sum()),
        co2_without_kg=float((schedule["load_kwh"] * carbon).sum() / 1000),
        co2_with_kg=float((schedule["grid_kwh"] * carbon).sum() / 1000),
        uses_forecast=not bool(forecast.loc[active, "price_published"].all()),
    )


def _battery_values(forecast, carbon_weight, risk_aversion):
    """Value per kWh of buying (charging) and saving (discharging) in each slot.

    Carbon is rescaled onto the price range so both are in pence and the
    wear cost stays meaningful. Charging is valued on the cautious upper
    band and discharging on the cautious lower band.
    """
    def cautious(prefix):
        low, mid, high = (forecast[f"{prefix}_{q}"] for q in ("p10", "p50", "p90"))
        return mid + risk_aversion * (high - mid), mid - risk_aversion * (mid - low)

    price_buy, price_sell = cautious("price")
    carbon_buy, carbon_sell = cautious("carbon")
    price_low = forecast["price_p50"].min()
    price_spread = max(forecast["price_p50"].max() - price_low, 1.0)
    carbon_low = forecast["carbon_p50"].min()
    carbon_spread = max(forecast["carbon_p50"].max() - carbon_low, 1.0)

    def as_pence(carbon):
        return price_low + (carbon - carbon_low) / carbon_spread * price_spread

    buy = (1 - carbon_weight) * price_buy + carbon_weight * as_pence(carbon_buy)
    sell = (1 - carbon_weight) * price_sell + carbon_weight * as_pence(carbon_sell)
    return buy.to_numpy(), sell.to_numpy()
