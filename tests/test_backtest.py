"""Regression checks for realised scoring, chronology and replay accounting."""

import unittest

import numpy as np
import pandas as pd

from src import backtest, calibration


class BacktestTests(unittest.TestCase):
    def test_clock_change_day_has_25_hours(self):
        cutoff = pd.Timestamp("2026-10-24 14:00", tz="UTC")
        end = backtest.next_local_time(cutoff, 15)
        self.assertEqual(end - cutoff, pd.Timedelta(hours=25))
        self.assertEqual(backtest.next_local_time(cutoff, 7),
                         pd.Timestamp("2026-10-25 07:00", tz="UTC"))

    def test_realised_cost_includes_negative_prices_and_units(self):
        index = pd.date_range("2026-01-01", periods=2, freq="30min", tz="UTC")
        actual = backtest.point_table(index, [-10, 30], [100, 200])
        cost, carbon = backtest.realised_totals(actual, index, [1, 2])
        self.assertAlmostEqual(cost, 0.5)
        self.assertAlmostEqual(carbon, 0.5)
        actual.iloc[0, actual.columns.get_loc("price_p50")] = np.nan
        with self.assertRaisesRegex(ValueError, "Missing actuals"):
            backtest.realised_totals(actual, index, [1, 2])

    def test_future_labels_cannot_change_rolling_offsets(self):
        cutoff = pd.Timestamp("2026-01-10", tz="UTC")
        pool = pd.DataFrame({
            "forecast_at_utc": [cutoff - pd.Timedelta(days=1)] * 200,
            "label_ready_utc": [cutoff - pd.Timedelta(hours=1)] * 100
            + [cutoff + pd.Timedelta(hours=1)] * 100,
            "actual": [10.0] * 200, "p10": [5.0] * 200,
            "p90": [15.0] * 200, "bucket": [0] * 200,
        })
        expected = calibration.rolling_offsets(pool, cutoff, {0: (0, 0, 1)})
        pool.loc[100:, "actual"] = 1e9
        self.assertEqual(calibration.rolling_offsets(pool, cutoff, {0: (0, 0, 1)}), expected)

    def test_oracle_bound_and_daily_energy_match(self):
        cutoff = pd.Timestamp("2026-01-01 15:00", tz="UTC")
        index = pd.date_range(cutoff, periods=96, freq="30min")
        actual = backtest.point_table(index, np.arange(96) % 17, np.full(96, 100))
        forecast = backtest.point_table(index, np.arange(96)[::-1], np.full(96, 100))
        rows = pd.DataFrame(backtest.appliance_receipts(cutoff, forecast, actual, forecast, 0, 0.5))
        for _, appliance in rows.groupby("appliance"):
            oracle = appliance.loc[appliance.strategy == "perfect_foresight", "cost_gbp"].iloc[0]
            self.assertTrue((appliance.cost_gbp >= oracle - 1e-10).all())
            self.assertEqual(appliance.energy_kwh.nunique(), 1)

    def test_drawdown_includes_initial_loss(self):
        result = backtest.pnl_metrics([-2, 1, -3, 5])
        self.assertEqual(result["max_drawdown_gbp"], 4)
        self.assertEqual(result["net_pnl_gbp"], 1)
        self.assertIsNone(backtest.pnl_metrics([0, 0])["annualised_daily_savings_sharpe"])

    def test_battery_preserves_charge_and_realised_accounting(self):
        cutoff = pd.Timestamp("2026-01-01 15:00", tz="UTC")
        index = pd.date_range(cutoff, periods=96, freq="30min")
        forecast = backtest.point_table(index, np.tile([5] * 24 + [30] * 24, 2), np.full(96, 100))
        actual = backtest.point_table(index, np.full(96, 20), np.full(96, 100))
        record, schedule, final = backtest.battery_receipt(cutoff, forecast, actual, 5, 0, 0)
        self.assertGreaterEqual(final, 5 - 1e-5)
        self.assertEqual(len(schedule), 48)
        self.assertAlmostEqual(record["with_battery_gbp"], schedule.grid_kwh.sum() * 0.2)
        self.assertAlmostEqual(record["pnl_gbp"], record["without_battery_gbp"]
                               - record["with_battery_gbp"] - record["degradation_gbp"])


if __name__ == "__main__":
    unittest.main()
