"""Step 5 regression tests: no leakage, correct evaluation, valid predictions."""

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from src.models import (chronological_split, evaluate_quantiles, numeric_features,
                        pinball, predict_quantiles)
from src.features import FEATURE_COLUMNS


class ModelTests(unittest.TestCase):
    @staticmethod
    def fake_frame(days=110):
        cutoffs = pd.date_range("2026-01-01 15:00", periods=days, freq="D", tz="UTC")
        issued = cutoffs.repeat(4)
        target = issued + pd.to_timedelta(np.tile([0,30,60,90], days), unit="m")
        frame = pd.DataFrame({"forecast_at_utc": issued, "target_at_utc": target,
                              "price_label_available_at_utc": target + pd.Timedelta(minutes=30),
                              "carbon_label_available_at_utc": target + pd.Timedelta(hours=3),
                              "label_price_p_kwh": np.ones(len(issued)) * 15,
                              "label_carbon_gco2_kwh": np.ones(len(issued)) * 120})
        for c in FEATURE_COLUMNS:
            frame[c] = 1.0
        return frame

    def test_purged_daily_splits(self):
        train, valid, test = chronological_split(self.fake_frame(), 20, 20, 52)
        self.assertLess(train.target_at_utc.max(), valid.forecast_at_utc.min())
        self.assertLess(valid.target_at_utc.max(), test.forecast_at_utc.min())
        self.assertLess(train.carbon_label_available_at_utc.max(), valid.forecast_at_utc.min())
        self.assertEqual(test.forecast_at_utc.nunique(), 20)

    def test_refuse_late_label_publication(self):
        f = self.fake_frame()
        f.loc[0, "carbon_label_available_at_utc"] = "2027-01-01T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "leakage"):
            chronological_split(f, 20, 20, 52)

    def test_only_explicit_features(self):
        f = self.fake_frame()
        f["future_label_secret"] = 1234567
        out = numeric_features(f)
        self.assertEqual(out.columns.tolist(), list(FEATURE_COLUMNS))
        self.assertNotIn("future_label_secret", out)

    def test_pinball_loss(self):
        self.assertAlmostEqual(pinball(np.array([10.]), np.array([8.]), .1), .2)
        self.assertAlmostEqual(pinball(np.array([10.]), np.array([12.]), .9), .2)

    def test_coverage_and_mae(self):
        f = pd.DataFrame({"label_carbon_gco2_kwh": [10., 20., 30.]})
        p = np.array([[0,10,20], [10,20,30], [15,25,28]], dtype=float)
        metrics = evaluate_quantiles(f, p, "carbon", "test")
        self.assertEqual(metrics["rows"], 3)
        self.assertAlmostEqual(metrics["coverage_80_pct"], 200/3)
        self.assertAlmostEqual(metrics["mae"], 5/3)

    def test_quantiles_sorted_and_carbon_nonnegative(self):
        class FakeModel:
            def __init__(self, value): self.value = value
            def predict(self, x): return np.full(len(x), self.value)
        f = self.fake_frame(2)
        p = predict_quantiles({.1:FakeModel(9), .5:FakeModel(-2), .9:FakeModel(4)}, f, carbon=True)
        self.assertEqual(p[0].tolist(), [0,4,9])


if __name__ == "__main__":
    unittest.main()
