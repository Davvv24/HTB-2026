"""Checks for explanation arithmetic, honest trade-offs and quantile crossing."""

import json
import unittest

import numpy as np
import pandas as pd

from src import explain, optimiser
from src.features import FEATURE_COLUMNS


START = pd.Timestamp("2026-11-01 15:00", tz="UTC")


class FakeModel:
    def __init__(self, values):
        self.values = np.asarray(values)

    def feature_name(self):
        return list(FEATURE_COLUMNS)

    def predict(self, frame, pred_contrib=False):
        if not pred_contrib:
            return self.values
        result = np.zeros((len(frame), len(FEATURE_COLUMNS) + 1))
        result[:, 0] = self.values
        return result


def forecast():
    index = pd.date_range(START, periods=4, freq="30min")
    table = pd.DataFrame(index=index)
    for q in ("p10", "p50", "p90"):
        table[f"price_{q}"] = [30., 5., 30., 5.]
        table[f"carbon_{q}"] = [100., 300., 100., 300.]
    table["price_published"] = False
    return table


def contributions(table):
    result = {}
    for source in ("price", "carbon"):
        values = table[f"{source}_p50"]
        result[source] = pd.DataFrame({"hour_of_day": values, "bias": 0.,
                                      "postprocess": 0., "prediction": values}, index=table.index)
    return result


class ExplanationTests(unittest.TestCase):
    def test_crossing_selects_contributions_of_actual_middle_prediction(self):
        features = pd.DataFrame(1., index=range(2), columns=FEATURE_COLUMNS)
        features["target_at_utc"] = pd.date_range(START, periods=2, freq="30min")
        fitted = {.1: FakeModel([9., -3.]), .5: FakeModel([2., -2.]), .9: FakeModel([4., -1.])}
        result = explain.median_contributions(fitted, features, carbon=True)
        np.testing.assert_allclose(result.prediction, [4., 0.])
        np.testing.assert_allclose(result.postprocess, [0., 2.])
        np.testing.assert_allclose(result.drop(columns="prediction").sum(axis=1), result.prediction)

    def test_ev_partial_slot_contributions_reconcile(self):
        table = forecast()
        plan = optimiser.plan_ev(table, 5, START, START + pd.Timedelta(hours=2), power_kw=7)
        result = explain.explain_recommendation(plan, table, contributions(table))
        self.assertAlmostEqual(result["model_drivers"]["price"]["recommended_minus_run_now"],
                               -plan.saving_p / 100)
        self.assertAlmostEqual(result["model_drivers"]["carbon"]["recommended_minus_run_now"],
                               -plan.saving_co2_kg)
        self.assertIn("kg CO2 more", result["why"])
        json.dumps(result, allow_nan=False)

    def test_published_prices_have_no_model_attribution(self):
        table = forecast()
        table["price_published"] = True
        plan = optimiser.plan_ev(table, 5, START, START + pd.Timedelta(hours=2))
        result = explain.explain_recommendation(plan, table, contributions(table))
        self.assertEqual(result["model_drivers"]["price"]["status"], "published_prices")
        self.assertEqual(result["model_drivers"]["price"]["drivers"], [])

    def test_different_model_predictions_are_labelled(self):
        table = forecast()
        plan = optimiser.plan_ev(table, 5, START, START + pd.Timedelta(hours=2))
        drivers = contributions(table)
        drivers["carbon"]["prediction"] += 1
        result = explain.explain_recommendation(plan, table, drivers)
        self.assertEqual(result["model_drivers"]["carbon"]["status"], "different_forecast_median")

    def test_stale_plan_is_rejected(self):
        table = forecast()
        plan = optimiser.plan_ev(table, 5, START, START + pd.Timedelta(hours=2))
        plan.cost_p += 100
        with self.assertRaisesRegex(ValueError, "totals"):
            explain.explain_recommendation(plan, table, contributions(table))

    def test_serialisation_adds_computed_explanation_for_each_plan(self):
        table = forecast()
        appliance = optimiser.Appliance("Test appliance", 1., 1)
        plan = optimiser.plan_appliance(table, appliance, START, START + pd.Timedelta(hours=2))
        self.assertIn("0.12 GBP less", plan.to_dict()["why"])
        self.assertIn("0.100 kg CO2 more", plan.to_dict()["why"])
        battery = optimiser.plan_battery(table)
        self.assertIn("after degradation", battery.to_dict()["why"])

    def test_ties_do_not_claim_savings(self):
        table = forecast()
        table.loc[:, ["price_p10", "price_p50", "price_p90"]] = 10.
        table.loc[:, ["carbon_p10", "carbon_p50", "carbon_p90"]] = 100.
        plan = optimiser.plan_ev(table, 5, START, START + pd.Timedelta(hours=2))
        self.assertIn("same GBP", plan.to_dict()["why"])
        self.assertIn("same kg CO2", plan.to_dict()["why"])


if __name__ == "__main__":
    unittest.main()
