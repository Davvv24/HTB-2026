"""Checks for the optimiser's forecast table, appliances and EV charging."""

import unittest

import pandas as pd

from src import optimiser


def agile_rows(start, prices):
    slots = pd.date_range(start, periods=len(prices), freq="30min", tz="UTC")
    return [
        {"valid_from": f"{slot:%Y-%m-%dT%H:%M:%SZ}", "price_p_kwh_inc_vat": price}
        for slot, price in zip(slots, prices)
    ]


def carbon_rows(start, values):
    slots = pd.date_range(start, periods=len(values), freq="30min", tz="UTC")
    return [{"from": f"{slot:%Y-%m-%dT%H:%MZ}", "forecast_gco2_kwh": value}
            for slot, value in zip(slots, values)]


class ForecastTableTests(unittest.TestCase):
    def test_builds_overlapping_slots_with_zero_width_bands(self):
        table = optimiser.make_forecast_from_published(
            agile_rows("2026-11-07 17:00", [30.0, 31.5, 29.0, 40.0]),
            carbon_rows("2026-11-07 17:30", [150, 140, 130, 120]),
        )
        self.assertEqual(len(table), 3)  # Only 17:30, 18:00 and 18:30 overlap.
        self.assertEqual(str(table.index[0]), "2026-11-07 17:30:00+00:00")
        self.assertEqual(table["price_p50"].tolist(), [31.5, 29.0, 40.0])
        self.assertTrue((table["price_p10"] == table["price_p90"]).all())
        self.assertTrue(table["price_published"].all())

    def test_reads_csv_strings_and_skips_blank_values(self):
        table = optimiser.make_forecast_from_published(
            agile_rows("2026-11-07 00:00", ["12.5", "13", ""]),
            carbon_rows("2026-11-07 00:00", ["100", "90", "80"]),
        )
        self.assertEqual(table["price_p50"].tolist(), [12.5, 13.0])

    def test_cuts_the_table_at_the_first_gap(self):
        prices = agile_rows("2026-11-07 00:00", [1.0, 2.0, 3.0, 4.0])
        del prices[2]  # No 01:00 price.
        table = optimiser.make_forecast_from_published(
            prices, carbon_rows("2026-11-07 00:00", [1, 2, 3, 4])
        )
        self.assertEqual(len(table), 2)

    def test_keeps_utc_slots_across_the_clocks_going_back(self):
        # 25 October 2026 has 50 local half-hours but stays 48 + 2 in UTC.
        start = "2026-10-24 23:00"
        table = optimiser.make_forecast_from_published(
            agile_rows(start, [10.0] * 50), carbon_rows(start, [100] * 50)
        )
        self.assertEqual(len(table), 50)
        local_days = table.tz_convert("Europe/London").index.date
        self.assertEqual(sum(day.day == 25 for day in local_days), 50)

    def test_rejects_a_band_in_the_wrong_order(self):
        table = optimiser.make_forecast_from_published(
            agile_rows("2026-11-07 00:00", [10.0]), carbon_rows("2026-11-07 00:00", [100])
        )
        table["carbon_p90"] = 50.0
        with self.assertRaises(optimiser.ForecastError):
            optimiser.validate_forecast(table)

    def test_rejects_an_empty_overlap(self):
        with self.assertRaises(optimiser.ForecastError):
            optimiser.make_forecast_from_published(
                agile_rows("2026-11-07 00:00", [10.0]), carbon_rows("2026-11-08 00:00", [100])
            )


def forecast(prices, carbon=None, start="2026-11-07 00:00", price_band=None):
    """A forecast table from lists. price_band adds p90 - p50 per slot."""
    slots = pd.date_range(start, periods=len(prices), freq="30min", tz="UTC", name="time_utc")
    carbon = carbon if carbon is not None else [100.0] * len(prices)
    band = price_band if price_band is not None else [0.0] * len(prices)
    table = pd.DataFrame(index=slots)
    table["price_p10"] = [p - b for p, b in zip(prices, band)]
    table["price_p50"] = prices
    table["price_p90"] = [p + b for p, b in zip(prices, band)]
    table["carbon_p10"] = table["carbon_p50"] = table["carbon_p90"] = carbon
    table["price_published"] = [b == 0 for b in band]
    return table


TWO_SLOT_KETTLE = optimiser.Appliance("Test appliance", 2.0, 2)  # 1 kWh per slot
START = pd.Timestamp("2026-11-07 00:00", tz="UTC")
LATE = pd.Timestamp("2026-11-08 00:00", tz="UTC")


class PlanApplianceTests(unittest.TestCase):
    def test_finds_the_cheapest_window_and_the_saving(self):
        plan = optimiser.plan_appliance(
            forecast([30, 30, 20, 10, 10, 25]), TWO_SLOT_KETTLE, START, LATE
        )
        self.assertEqual(plan.start_utc, START + 3 * optimiser.SLOT)
        self.assertEqual(plan.end_utc, START + 5 * optimiser.SLOT)
        self.assertAlmostEqual(plan.cost_p, 20.0)  # 10p x 1 kWh x 2 slots
        self.assertAlmostEqual(plan.run_now_cost_p, 60.0)
        self.assertAlmostEqual(plan.saving_p, 40.0)
        self.assertFalse(plan.uses_forecast)

    def test_respects_the_deadline(self):
        plan = optimiser.plan_appliance(
            forecast([30, 20, 25, 1, 1]), TWO_SLOT_KETTLE, START, START + 3 * optimiser.SLOT
        )
        # The 1p slots finish after the deadline, so 20 + 25 beats 30 + 20.
        self.assertLessEqual(plan.end_utc, START + 3 * optimiser.SLOT)
        self.assertEqual(plan.start_utc, START + optimiser.SLOT)

    def test_a_deadline_that_is_too_tight_raises(self):
        with self.assertRaisesRegex(optimiser.InfeasibleError, "needs 1 h, but only 0.5 h"):
            optimiser.plan_appliance(
                forecast([10, 10, 10]), TWO_SLOT_KETTLE, START, START + optimiser.SLOT
            )

    def test_an_earliest_time_after_the_forecast_raises(self):
        with self.assertRaisesRegex(optimiser.InfeasibleError, "only runs until"):
            optimiser.plan_appliance(
                forecast([10, 10]), TWO_SLOT_KETTLE, START + 2 * optimiser.SLOT, LATE
            )

    def test_rounds_the_earliest_start_up_to_the_next_slot(self):
        plan = optimiser.plan_appliance(
            forecast([1, 1, 50, 50, 50]), TWO_SLOT_KETTLE, pd.Timestamp("2026-11-07 00:10", tz="UTC"), LATE
        )
        self.assertEqual(plan.start_utc, START + optimiser.SLOT)

    def test_the_slider_moves_from_cheapest_to_greenest(self):
        table = forecast([10, 10, 40, 40], carbon=[300, 300, 50, 50])
        cheapest = optimiser.plan_appliance(table, TWO_SLOT_KETTLE, START, LATE, carbon_weight=0)
        greenest = optimiser.plan_appliance(table, TWO_SLOT_KETTLE, START, LATE, carbon_weight=1)
        self.assertEqual(cheapest.start_utc, START)
        self.assertEqual(greenest.start_utc, START + 2 * optimiser.SLOT)
        self.assertAlmostEqual(greenest.saving_co2_kg, 0.5)  # (300 - 50) g x 2 kWh

    def test_risk_aversion_avoids_an_uncertain_window(self):
        # Slots 0-1 are a known 20p. Slots 2-3 are forecast at 18p but could reach 38p.
        table = forecast([20, 20, 18, 18], price_band=[0, 0, 20, 20])
        bold = optimiser.plan_appliance(table, TWO_SLOT_KETTLE, START, LATE, risk_aversion=0)
        careful = optimiser.plan_appliance(table, TWO_SLOT_KETTLE, START, LATE, risk_aversion=1)
        self.assertEqual(bold.start_utc, START + 2 * optimiser.SLOT)
        self.assertTrue(bold.uses_forecast)
        self.assertEqual(careful.start_utc, START)
        self.assertFalse(careful.uses_forecast)

    def test_a_flat_forecast_recommends_running_now(self):
        plan = optimiser.plan_appliance(forecast([15] * 6), TWO_SLOT_KETTLE, START, LATE)
        self.assertEqual(plan.start_utc, START)
        self.assertEqual(plan.saving_p, 0)

    def test_alternatives_do_not_overlap(self):
        plan = optimiser.plan_appliance(
            forecast([5, 6, 7, 8, 9, 10, 11, 12]), TWO_SLOT_KETTLE, START, LATE
        )
        starts = [plan.start_utc] + [alt.start_utc for alt in plan.alternatives]
        self.assertEqual(len(starts), 3)
        for a in starts:
            for b in starts:
                if a != b:
                    self.assertGreaterEqual(abs(a - b), 2 * optimiser.SLOT)

    def test_rejects_times_without_a_time_zone(self):
        with self.assertRaises(ValueError):
            optimiser.plan_appliance(
                forecast([10, 10]), TWO_SLOT_KETTLE, pd.Timestamp("2026-11-07"), LATE
            )

    def test_presets_load_from_config(self):
        washer = optimiser.get_appliance("washing_machine")
        self.assertEqual(washer.duration_slots * washer.energy_per_slot_kwh, 1.0)


def at(slot):
    return START + slot * optimiser.SLOT


class PlanEvTests(unittest.TestCase):
    # A 7 kW charger delivers 3.5 kWh per half-hour.

    def test_picks_the_cheapest_slots_even_when_apart(self):
        plan = optimiser.plan_ev(forecast([30, 5, 30, 5, 30, 5]), 7, START, LATE, power_kw=7)
        self.assertEqual(plan.slots[:2], [at(1), at(3)])
        self.assertEqual(len(plan.slots), 2)
        self.assertEqual(plan.blocks(), [(at(1), at(2)), (at(3), at(4))])
        self.assertAlmostEqual(plan.cost_p, 35.0)

    def test_tops_up_in_the_last_slot_and_compares_with_charging_now(self):
        plan = optimiser.plan_ev(forecast([30, 5, 30, 5]), 5, START, LATE, power_kw=7)
        self.assertEqual(plan.slots, [at(1), at(3)])
        self.assertEqual(plan.slot_energy_kwh, [3.5, 1.5])
        self.assertAlmostEqual(sum(plan.slot_energy_kwh), 5.0)
        self.assertAlmostEqual(plan.cost_p, 25.0)  # 5p x 5 kWh
        self.assertAlmostEqual(plan.run_now_cost_p, 112.5)  # 30p x 3.5 + 5p x 1.5
        self.assertAlmostEqual(plan.saving_p, 87.5)

    def test_an_exact_multiple_uses_whole_slots(self):
        plan = optimiser.plan_ev(forecast([10] * 6), 10.5, START, LATE, power_kw=7)
        self.assertEqual(plan.slot_energy_kwh, [3.5, 3.5, 3.5])

    def test_respects_the_deadline(self):
        plan = optimiser.plan_ev(forecast([30, 5, 30, 1]), 7, START, at(3), power_kw=7)
        self.assertLessEqual(plan.end_utc, at(3))
        self.assertEqual(plan.slots, [at(0), at(1)])  # 1p slot is too late; 30p tie goes earlier

    def test_not_enough_time_raises(self):
        with self.assertRaisesRegex(optimiser.InfeasibleError, "needs 2 h, but only 1.5 h"):
            optimiser.plan_ev(forecast([10] * 6), 14, START, at(3), power_kw=7)

    def test_greenest_charges_in_the_cleanest_slots(self):
        table = forecast([10, 10, 40, 40], carbon=[300, 300, 50, 50])
        plan = optimiser.plan_ev(table, 7, START, LATE, power_kw=7, carbon_weight=1)
        self.assertEqual(plan.slots, [at(2), at(3)])
        self.assertAlmostEqual(plan.saving_co2_kg, 1.75)  # 250 g x 7 kWh

    def test_uses_the_configured_charger_by_default(self):
        plan = optimiser.plan_ev(forecast([10] * 4), 7, START, LATE)
        self.assertEqual(plan.slot_energy_kwh, [3.5, 3.5])

    def test_rejects_zero_energy(self):
        with self.assertRaises(ValueError):
            optimiser.plan_ev(forecast([10] * 4), 0, START, LATE)


if __name__ == "__main__":
    unittest.main()
