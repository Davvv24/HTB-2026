import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import pandas as pd
from pydantic import ValidationError

from app.main import RecommendRequest, recommend
from src import calibration, optimiser
import test_models
from test_optimiser import forecast, load, START


class ValidationFixTests(unittest.TestCase):
    def test_negative_price_cannot_cycle_within_one_slot(self):
        plan = optimiser.plan_battery(forecast([-100]), load([0.5]),
                                      capacity_kwh=1, initial_kwh=1, power_kw=5)
        self.assertAlmostEqual(plan.schedule.charge_kwh.sum(), 0)
        self.assertAlmostEqual(plan.schedule.discharge_kwh.sum(), 0)

    def test_nonfinite_forecasts_and_settings_are_rejected(self):
        for value in [float('inf'), float('-inf'), float('nan')]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    optimiser.validate_forecast(forecast([value]))
                with self.assertRaises(ValueError):
                    optimiser.plan_battery(forecast([10]), risk_aversion=value)
                with self.assertRaises(ValueError):
                    optimiser.plan_ev(forecast([10]), value, START, START + pd.Timedelta(hours=1))

    def test_api_rejects_invalid_requests(self):
        valid = dict(appliance='ev', earliest_start='2026-11-03T15:00:00Z',
                     deadline='2026-11-04T07:00:00Z')
        for changes in [dict(ev_kwh_needed=0), dict(ev_kwh_needed=-1),
                        dict(carbon_weight=2), dict(risk_aversion=float('inf')),
                        dict(earliest_start='2026-11-03T15:00:00'),
                        dict(deadline='2026-11-03T14:00:00Z')]:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                RecommendRequest(**{**valid, **changes})

    @mock.patch.dict(os.environ, {'GRID_ADVISOR_LIVE': '0'})  # Historical replay only
    def test_api_respects_ev_energy(self):
        result = recommend(RecommendRequest(
            appliance='ev', earliest_start='2026-11-03T15:00:00Z',
            deadline='2026-11-04T07:00:00Z', ev_kwh_needed=5))
        self.assertEqual(result['energy_kwh'], 5)
        self.assertTrue(result['historical_demo'])

    def test_calibration_rejects_mismatched_training_period(self):
        frame = test_models.ModelTests.fake_frame(120)
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'training.json').write_text(json.dumps({
                'last_training_cutoff': '2026-12-01T00:00:00Z'}))
            with self.assertRaisesRegex(ValueError, 'training period'):
                calibration.rebuild_predictions(frame, directory, 20, 20)


if __name__ == '__main__':
    unittest.main()
