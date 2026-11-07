"""Checks for Step 6 conformal calibration on synthetic forecasts."""

import unittest

import numpy as np
import pandas as pd

from src import calibration

START = pd.Timestamp("2026-08-01", tz="UTC")


def long_table(n, *, seed=0, sd=2.0, half_width=1.0, start=START, ready_after_hours=1, actual=None):
    """Forecasts every 30 min with a band of +/- half_width around 0."""
    rng = np.random.default_rng(seed)
    times = start + pd.to_timedelta(np.arange(n) * 30, unit="min")
    values = rng.normal(0, sd, n) if actual is None else np.full(n, float(actual))
    return pd.DataFrame({
        "forecast_at_utc": times,
        "horizon_minutes": 0,
        "bucket": 0,
        "actual": values,
        "label_ready_utc": times + pd.to_timedelta(ready_after_hours, unit="h"),
        "p10": -half_width,
        "p50": 0.0,
        "p90": half_width,
    })


class ConformalQuantileTests(unittest.TestCase):
    def test_uses_the_finite_sample_rank(self):
        self.assertEqual(calibration.conformal_quantile(range(1, 10)), 9)  # ceil(10 x 0.9) = 9th
        self.assertEqual(calibration.conformal_quantile(range(1, 100)), 90)  # ceil(100 x 0.9) = 90th

    def test_ignores_missing_values_and_rejects_none(self):
        self.assertEqual(calibration.conformal_quantile([np.nan, 1, 2, 3]), 3)
        with self.assertRaises(ValueError):
            calibration.conformal_quantile([np.nan])

    def test_buckets_are_twelve_hours(self):
        self.assertEqual(calibration.horizon_bucket([0, 690, 720, 2850]).tolist(), [0, 0, 1, 3])
        self.assertEqual(calibration.bucket_label(1), "12-24h")


class FixedCalibrationTests(unittest.TestCase):
    def test_widens_a_too_narrow_band_to_about_80_percent(self):
        calib = long_table(3000, seed=1)
        test = long_table(4000, seed=2)
        raw = calibration.coverage(test)
        self.assertLess(raw["coverage_80_pct"], 50)  # +/-1 around N(0, 2) covers ~38%

        fixed = calibration.fixed_offsets(calib)
        calibrated = calibration.coverage(calibration.apply_offsets(test, fixed))
        self.assertAlmostEqual(calibrated["coverage_80_pct"], 80, delta=3)
        self.assertAlmostEqual(calibrated["below_p10_pct"], 10, delta=2)
        self.assertAlmostEqual(calibrated["above_p90_pct"], 10, delta=2)

    def test_keeps_quantiles_in_order_and_respects_the_floor(self):
        rows = long_table(10)
        shrink = {0: (-5.0, -5.0, 10)}  # Offsets that would cross p10 and p90 over p50
        out = calibration.apply_offsets(rows, shrink)
        self.assertTrue((out["p10"] <= out["p50"]).all() and (out["p50"] <= out["p90"]).all())
        widen = {0: (50.0, 1.0, 10)}
        out = calibration.apply_offsets(rows.assign(p50=10.0, p90=12.0), widen, floor=0.0)
        self.assertTrue((out["p10"] >= 0).all())


class RollingCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.fallback = {0: (0.0, 0.0, 0)}
        self.cutoff = START + pd.Timedelta(days=10)

    def test_ignores_outcomes_published_after_the_cutoff(self):
        calm = long_table(300, seed=3, sd=0.5)
        # Huge misses forecast before the cutoff but only published after it.
        late = long_table(300, start=START + pd.Timedelta(days=4), actual=100.0, ready_after_hours=24 * 30)
        offsets = calibration.rolling_offsets(pd.concat([calm, late]), self.cutoff, self.fallback)
        self.assertLess(offsets[0][1], 5)

        published = late.assign(label_ready_utc=late["forecast_at_utc"])
        offsets = calibration.rolling_offsets(pd.concat([calm, published]), self.cutoff, self.fallback)
        self.assertGreater(offsets[0][1], 50)

    def test_ignores_outcomes_older_than_the_window(self):
        old = long_table(300, start=self.cutoff - pd.Timedelta(days=40), actual=100.0)
        recent = long_table(300, start=self.cutoff - pd.Timedelta(days=7), seed=4, sd=0.5)
        offsets = calibration.rolling_offsets(pd.concat([old, recent]), self.cutoff, self.fallback)
        self.assertLess(offsets[0][1], 5)

    def test_falls_back_when_there_are_too_few_outcomes(self):
        few = long_table(calibration.MIN_SCORES - 1, start=START + pd.Timedelta(days=5))
        offsets = calibration.rolling_offsets(few, self.cutoff, self.fallback)
        self.assertIs(offsets[0], self.fallback[0])


if __name__ == "__main__":
    unittest.main()
