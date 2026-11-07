"""Regression checks for Step 4, including unavailable data and UK clock changes."""

import unittest

import numpy as np
import pandas as pd

from src.baselines import forecast_cutoffs, make_predictions, score_baselines
from src.build_dataset import CARBON, PRICE


class BaselinesTests(unittest.TestCase):
    @staticmethod
    def history(start="2026-10-15", end="2026-11-07"):
        times = pd.date_range(start, end, freq="30min", inclusive="left", tz="UTC")
        return pd.DataFrame({
            "time_utc": times,
            PRICE: np.arange(len(times), dtype=float),
            CARBON: np.arange(len(times), dtype=float) + 100,
            "gb_carbon_actual_suspect": False,
        })

    def test_never_uses_future_yesterday_observation(self):
        data = self.history()
        cutoff = pd.Timestamp("2026-11-02T15:00:00Z")
        pred = make_predictions(data, pd.DatetimeIndex([cutoff]))
        before = pred.loc[pred.target_at_utc == pd.Timestamp("2026-11-03T14:00:00Z")].iloc[0]
        after = pred.loc[pred.target_at_utc == pd.Timestamp("2026-11-03T15:00:00Z")].iloc[0]
        self.assertTrue(np.isfinite(before.price_yesterday))
        self.assertTrue(np.isnan(after.price_yesterday))
        self.assertTrue(np.isfinite(after.price_last_week))

    def test_carbon_requires_two_hour_assumed_publication_delay(self):
        pred = make_predictions(self.history(), pd.DatetimeIndex([pd.Timestamp("2026-11-02T15:00:00Z")]))
        earlier = pred.loc[pred.target_at_utc == pd.Timestamp("2026-11-03T12:30:00Z")].iloc[0]
        later = pred.loc[pred.target_at_utc == pd.Timestamp("2026-11-03T13:00:00Z")].iloc[0]
        self.assertTrue(np.isfinite(earlier.carbon_yesterday))
        self.assertTrue(np.isnan(later.carbon_yesterday))

    def test_daylight_saving_lag_uses_local_clock(self):
        # On 25 October 2026, 01:00 UK local occurs twice; both map to
        # the 01:00 local clock slot seven days earlier, rather than UTC-168h.
        data = self.history()
        cutoff = pd.Timestamp("2026-10-24T14:00:00Z")  # 15:00 BST
        pred = make_predictions(data, pd.DatetimeIndex([cutoff]), 24)
        first = pred.loc[pred.target_at_utc == pd.Timestamp("2026-10-25T00:00:00Z")].iloc[0]
        second = pred.loc[pred.target_at_utc == pd.Timestamp("2026-10-25T01:00:00Z")].iloc[0]
        self.assertEqual(first.price_last_week, second.price_last_week)
        self.assertEqual(first.price_yesterday, second.price_yesterday)

    def test_suspect_carbon_ignored(self):
        data = self.history()
        bad_time = pd.Timestamp("2026-11-01T15:00:00Z")
        data.loc[data.time_utc == bad_time, "gb_carbon_actual_suspect"] = True
        pred = make_predictions(data, pd.DatetimeIndex([pd.Timestamp("2026-11-02T15:00:00Z")]))
        row = pred.loc[pred.target_at_utc == pd.Timestamp("2026-11-02T15:00:00Z")].iloc[0]
        self.assertTrue(np.isnan(row.carbon_yesterday))
        self.assertTrue(np.isfinite(row.price_yesterday))

    def test_common_subset_scoring_is_fair(self):
        pred = pd.DataFrame({
            "actual_price": [10., 12., 20.],
            "price_yesterday": [9., np.nan, 18.],
            "price_last_week": [8., 100., 19.],
            "actual_carbon": [100., 200., 300.],
            "carbon_yesterday": [90., np.nan, 310.],
            "carbon_last_week": [80., 5., 320.],
        })
        result = score_baselines(pred).set_index(["target", "baseline"])
        self.assertAlmostEqual(result.loc[("price", "yesterday"), "mae"], 1.5)
        self.assertAlmostEqual(result.loc[("price", "last_week"), "mae"], 1.5)
        self.assertAlmostEqual(result.loc[("price", "yesterday"), "rmse"], np.sqrt(2.5))
        self.assertEqual(result.loc[("price", "last_week"), "available_slots"], 3)
        self.assertEqual(result.loc[("price", "last_week"), "common_slots"], 2)

    def test_daily_cutoffs_are_in_local_time(self):
        data = self.history()
        cutoffs = forecast_cutoffs(data, 24)
        self.assertIn(pd.Timestamp("2026-10-24T14:00:00Z"), cutoffs)
        self.assertIn(pd.Timestamp("2026-10-25T15:00:00Z"), cutoffs)


if __name__ == "__main__":
    unittest.main()
