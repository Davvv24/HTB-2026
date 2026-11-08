"""Checks for Step 11 live data: caching, fallbacks and API wiring, with no network."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from src import data, live

NOW = pd.Timestamp("2026-11-08 10:10", tz="UTC")  # Current slot starts 10:00


def agile_rows(start, prices):
    slots = pd.date_range(start, periods=len(prices), freq="30min", tz="UTC")
    return [{"valid_from": f"{t:%Y-%m-%dT%H:%M:%SZ}", "price_p_kwh_inc_vat": p} for t, p in zip(slots, prices)]


def carbon_rows(start, values, actual=None):
    slots = pd.date_range(start, periods=len(values), freq="30min", tz="UTC")
    actual = actual or [None] * len(values)
    return [{"from": f"{t:%Y-%m-%dT%H:%MZ}", "forecast_gco2_kwh": v, "actual_gco2_kwh": a}
            for t, v, a in zip(slots, values, actual)]


class LiveTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patches = [
            mock.patch.object(live, "LIVE_DIR", Path(self.tmp.name)),
            mock.patch.dict(os.environ, {"GRID_ADVISOR_LIVE": "1"}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        # No model forecast unless a test provides one, so nothing downloads.
        self.no_model = mock.patch.object(live, "model_snapshot", return_value=None)
        self.no_model.start()
        self.addCleanup(lambda: self.no_model.stop() if self.no_model_active else None)
        self.no_model_active = True
        self.addCleanup(self.tmp.cleanup)
        live.clear_memory()
        self.addCleanup(live.clear_memory)

    def published(self, prices=None):
        prices = prices or [20.0 + i for i in range(26)]  # 10:00 to 23:00
        return (
            mock.patch.object(data, "fetch_agile_rates", return_value=agile_rows("2026-11-08 10:00", prices)),
            mock.patch.object(data, "fetch_carbon", return_value=carbon_rows("2026-11-08 10:00", [150] * 96)),
        )


class ForecastTests(LiveTestCase):
    def test_builds_todays_table_from_published_data(self):
        agile, carbon = self.published()
        with agile as fetch_agile, carbon:
            snapshot = live.current_forecast(NOW)
        self.assertEqual(snapshot.source, "published_only")
        self.assertFalse(snapshot.cached)
        self.assertEqual(snapshot.table.index[0], pd.Timestamp("2026-11-08 10:00", tz="UTC"))
        self.assertEqual(len(snapshot.table), 26)  # Stops where published prices stop
        self.assertTrue(snapshot.table["price_published"].all())
        self.assertEqual(fetch_agile.call_args.args[2], NOW.to_pydatetime())
        meta = snapshot.meta()
        self.assertFalse(meta["historical_demo"])
        self.assertEqual(meta["last_updated"], "2026-11-08T10:10:00Z")

    def test_reuses_memory_within_ten_minutes(self):
        agile, carbon = self.published()
        with agile as fetch_agile, carbon:
            live.current_forecast(NOW)
            live.current_forecast(NOW + pd.Timedelta(minutes=5))
            self.assertEqual(fetch_agile.call_count, 1)
            later = live.current_forecast(NOW + pd.Timedelta(minutes=11))
            self.assertEqual(fetch_agile.call_count, 2)
        self.assertEqual(later.table.index[0], pd.Timestamp("2026-11-08 10:00", tz="UTC"))

    def test_drops_slots_that_have_passed(self):
        agile, carbon = self.published()
        with agile, carbon:
            live.current_forecast(NOW)
            snapshot = live.current_forecast(NOW + pd.Timedelta(minutes=9, seconds=59))
        self.assertEqual(snapshot.table.index[0], pd.Timestamp("2026-11-08 10:00", tz="UTC"))
        with agile, carbon:
            snapshot = live.current_forecast(NOW + pd.Timedelta(hours=2))
        self.assertEqual(snapshot.table.index[0], pd.Timestamp("2026-11-08 12:00", tz="UTC"))

    def test_falls_back_to_the_saved_copy_when_a_provider_fails(self):
        agile, carbon = self.published()
        with agile, carbon:
            live.current_forecast(NOW)
        live.clear_memory()
        failing = mock.patch.object(data, "fetch_agile_rates", side_effect=data.DataFetchError("down"))
        with failing:
            snapshot = live.current_forecast(NOW + pd.Timedelta(hours=1))
        self.assertTrue(snapshot.cached)
        self.assertEqual(snapshot.source, "published_only")
        self.assertEqual(snapshot.table.index[0], pd.Timestamp("2026-11-08 11:00", tz="UTC"))
        self.assertTrue(snapshot.meta()["cached"])

    def test_returns_none_when_the_saved_copy_has_run_out(self):
        agile, carbon = self.published()
        with agile, carbon:
            live.current_forecast(NOW)
        live.clear_memory()
        with mock.patch.object(data, "fetch_agile_rates", side_effect=data.DataFetchError("down")):
            self.assertIsNone(live.current_forecast(NOW + pd.Timedelta(hours=20)))

    def test_retries_a_minute_after_a_failure(self):
        failing = mock.patch.object(data, "fetch_agile_rates", side_effect=data.DataFetchError("down"))
        with failing as fetch_agile:
            self.assertIsNone(live.current_forecast(NOW))
            live.current_forecast(NOW + pd.Timedelta(seconds=30))
            self.assertEqual(fetch_agile.call_count, 1)
            live.current_forecast(NOW + pd.Timedelta(minutes=2))
            self.assertEqual(fetch_agile.call_count, 2)

    def test_server_serves_stale_data_while_refreshing_in_the_background(self):
        agile, carbon = self.published()
        with agile as fetch_agile, carbon:
            first = live.current_forecast(NOW)
            with mock.patch.object(live, "_serving", True), \
                    mock.patch.object(live, "_refresh_later") as refresh_later:
                stale = live.current_forecast(NOW + pd.Timedelta(minutes=11))
            self.assertEqual(fetch_agile.call_count, 1)  # The request did not wait for a fetch
        refresh_later.assert_called_once()
        self.assertEqual(stale.last_updated, first.last_updated)

    def test_background_refresh_does_not_start_when_live_data_is_off(self):
        with mock.patch.dict(os.environ, {"GRID_ADVISOR_LIVE": "0"}), \
                mock.patch.object(live.threading, "Thread") as thread:
            live.start_background_refresh()
        thread.assert_not_called()

    def test_cold_server_request_does_not_wait_for_providers(self):
        with mock.patch.object(live, "_serving", True), \
                mock.patch.object(live, "_refresh_later") as refresh, \
                mock.patch.object(live, "_refresh_forecast") as build:
            self.assertIsNone(live.current_forecast(NOW))
        refresh.assert_called_once()
        build.assert_not_called()

    def test_can_be_switched_off(self):
        agile, carbon = self.published()
        with mock.patch.dict(os.environ, {"GRID_ADVISOR_LIVE": "0"}), agile as fetch_agile, carbon:
            self.assertIsNone(live.current_forecast(NOW))
            self.assertIsNone(live.current_grid(NOW))
        fetch_agile.assert_not_called()


class GridTests(LiveTestCase):
    def mix_rows(self, start, count, gas=40.0):
        slots = pd.date_range(start, periods=count, freq="30min", tz="UTC")
        return [{"from": f"{t:%Y-%m-%dT%H:%MZ}", "wind_pct": 30.0, "gas_pct": gas, "nuclear_pct": 15.0}
                for t in slots]

    def test_uses_the_latest_trustworthy_reading(self):
        # 09:30 has no actual yet and 09:00 is a suspect zero, so 08:30 is used.
        carbon = carbon_rows("2026-11-08 08:30", [1, 1, 1], actual=[180, 0, None])
        with mock.patch.object(data, "fetch_carbon", return_value=carbon), \
                mock.patch.object(data, "fetch_generation_mix", return_value=self.mix_rows("2026-11-08 08:30", 3)):
            grid = live.current_grid(NOW)
        self.assertEqual(grid["carbon_intensity"], 180)
        self.assertEqual(grid["data_as_of"], "2026-11-08T08:30:00Z")
        self.assertEqual(grid["mix_percent"], {"wind": 30.0, "gas": 40.0, "nuclear": 15.0})
        self.assertFalse(grid["historical_demo"])
        self.assertFalse(grid["cached"])
        self.assertTrue((Path(self.tmp.name) / "grid.json").is_file())

    def test_uses_a_recent_saved_reading_when_neso_fails(self):
        saved = {"data_as_of": "2026-11-08T08:30:00Z", "cached": False, "carbon_intensity": 180}
        (Path(self.tmp.name) / "grid.json").write_text(json.dumps(saved))
        with mock.patch.object(data, "fetch_carbon", side_effect=data.DataFetchError("down")):
            grid = live.current_grid(NOW)
        self.assertTrue(grid["cached"])
        self.assertEqual(grid["carbon_intensity"], 180)

    def test_ignores_a_saved_reading_older_than_six_hours(self):
        saved = {"data_as_of": "2026-11-08T01:00:00Z", "cached": False, "carbon_intensity": 180}
        (Path(self.tmp.name) / "grid.json").write_text(json.dumps(saved))
        with mock.patch.object(data, "fetch_carbon", side_effect=data.DataFetchError("down")):
            self.assertIsNone(live.current_grid(NOW))


def model_snapshot(cutoff, prices=None):
    """A fake model forecast: 96 forecast slots from `cutoff` with a 10p-wide price band."""
    slots = pd.date_range(cutoff, periods=96, freq="30min", tz="UTC", name="time_utc")
    prices = prices if prices is not None else [25.0] * 96
    table = pd.DataFrame({
        "price_p10": [p - 5 for p in prices], "price_p50": prices, "price_p90": [p + 5 for p in prices],
        "carbon_p10": 100.0, "carbon_p50": 150.0, "carbon_p90": 200.0, "price_published": False,
    }, index=slots)
    return live.ForecastSnapshot(table, data_as_of=pd.Timestamp(cutoff), last_updated=pd.Timestamp(cutoff),
                                 source="model")


class CutoffTests(unittest.TestCase):
    def test_uses_todays_15_00_after_it_and_yesterdays_before(self):
        self.assertEqual(live.latest_cutoff(pd.Timestamp("2026-11-08 16:00", tz="UTC")),
                         pd.Timestamp("2026-11-08 15:00", tz="UTC"))
        self.assertEqual(live.latest_cutoff(pd.Timestamp("2026-11-08 10:10", tz="UTC")),
                         pd.Timestamp("2026-11-07 15:00", tz="UTC"))

    def test_follows_uk_summer_time(self):
        # 15:00 BST is 14:00 UTC, so 14:30 UTC is already after the cutoff.
        self.assertEqual(live.latest_cutoff(pd.Timestamp("2026-07-01 14:30", tz="UTC")),
                         pd.Timestamp("2026-07-01 14:00", tz="UTC"))
        # On the day the clocks go back, the morning still uses the summer-time cutoff.
        self.assertEqual(live.latest_cutoff(pd.Timestamp("2026-10-25 09:00", tz="UTC")),
                         pd.Timestamp("2026-10-24 14:00", tz="UTC"))


class PredictTableTests(unittest.TestCase):
    """Uses the committed Step 5 models, Step 6 offsets and Step 3 features, offline."""

    @classmethod
    def setUpClass(cls):
        rows = pd.read_parquet(live.config.DATA_DIR / "features.parquet")
        cutoff = pd.to_datetime(rows["forecast_at_utc"], utc=True).max()
        cls.rows = rows[pd.to_datetime(rows["forecast_at_utc"], utc=True) == cutoff].reset_index(drop=True)
        cls.table = live.predict_table(cls.rows)
        cls.offsets = json.loads(live.OFFSETS_PATH.read_text())

    def test_returns_a_valid_48_hour_table(self):
        self.assertEqual(len(self.table), 96)
        self.assertFalse(self.table["price_published"].any())
        self.assertTrue((self.table["carbon_p10"] >= 0).all())

    def test_widens_each_band_by_its_horizon_offset(self):
        raw = live.models.predict_quantiles(live.models.load_models(live.MODEL_DIR, "price"), self.rows)
        self.assertEqual(self.offsets["price"]["deployment_method"], "adaptive_latest")
        self.assertEqual(self.offsets["carbon"]["deployment_method"], "rolling_latest")
        first = self.offsets["price"]["adaptive_latest"]["0-12h"]
        expected_p90 = max(raw[0, 2] + first["upper"], raw[0, 1])
        self.assertAlmostEqual(self.table["price_p90"].iloc[0], expected_p90, places=6)
        last = self.offsets["price"]["adaptive_latest"]["36-48h"]
        self.assertAlmostEqual(self.table["price_p10"].iloc[-1],
                               min(raw[-1, 0] - last["lower"], raw[-1, 1]), places=6)

    def test_shifts_a_biased_price_median_inside_the_band(self):
        raw = live.models.predict_quantiles(live.models.load_models(live.MODEL_DIR, "price"), self.rows)
        shift = self.offsets["price"]["median_shift"]["0-12h"]
        self.assertGreater(shift, 0)  # The price model's median runs low
        row = self.table.iloc[0]
        expected = min(max(raw[0, 1] + shift, row["price_p10"]), row["price_p90"])
        self.assertAlmostEqual(row["price_p50"], expected, places=6)
        self.assertTrue((self.table["price_p10"] <= self.table["price_p50"]).all())
        self.assertTrue((self.table["price_p50"] <= self.table["price_p90"]).all())
        # Carbon's median was already unbiased, so it is served unshifted.
        self.assertNotIn("median_shift", self.offsets["carbon"])

    def test_explanations_add_up_to_the_shifted_median(self):
        contributions = live.reconcile_contributions(
            live.explain.model_explanations(self.rows, live.MODEL_DIR), self.table)
        price = contributions["price"]
        self.assertIn("calibration_shift", price)
        np.testing.assert_allclose(price.drop(columns="prediction").sum(axis=1), price["prediction"])
        np.testing.assert_allclose(price["prediction"].to_numpy(), self.table["price_p50"].to_numpy())
        self.assertNotIn("calibration_shift", contributions["carbon"])


class ModelForecastTests(LiveTestCase):
    def test_expired_model_does_not_hide_fresh_published_prices(self):
        expired = model_snapshot(pd.Timestamp("2026-11-04 15:00", tz="UTC"))
        agile, carbon = self.published()
        with mock.patch.object(live, "model_snapshot", return_value=expired), agile, carbon:
            snapshot = live.current_forecast(NOW)
        self.assertEqual(snapshot.source, "published_only")
        self.assertEqual(len(snapshot.table), 26)
        self.assertTrue(snapshot.table.price_published.all())

    def test_changed_calibration_invalidates_saved_served_model(self):
        saved = model_snapshot(live.latest_cutoff(NOW))
        live._save_forecast(saved)
        with mock.patch.object(live, "calibration_fingerprint", return_value="changed"):
            self.assertIsNone(live._saved_forecast())

    def test_changed_calibration_invalidates_saved_raw_model(self):
        saved = model_snapshot(live.latest_cutoff(NOW))
        saved.table.to_parquet(live.LIVE_DIR / "model_forecast.parquet")
        (live.LIVE_DIR / "model_meta.json").write_text(json.dumps({
            "cutoff": live.iso(saved.data_as_of), "built_at": live.iso(NOW),
            "calibration_sha256": "old"}))
        self.assertIsNone(live._saved_model())

    def use_real_model_snapshot(self):
        self.no_model.stop()
        self.no_model_active = False

    def test_published_prices_replace_model_prices_where_known(self):
        cutoff = live.latest_cutoff(NOW)
        agile, carbon = self.published()
        with mock.patch.object(live, "model_snapshot", return_value=model_snapshot(cutoff)), agile, carbon:
            snapshot = live.current_forecast(NOW)
        table = snapshot.table
        self.assertEqual(snapshot.source, "model")
        self.assertEqual(snapshot.data_as_of, cutoff)  # Issued at yesterday's 15:00
        self.assertEqual(table.index[0], pd.Timestamp("2026-11-08 10:00", tz="UTC"))
        published = table[table["price_published"]]
        self.assertEqual(len(published), 26)  # 10:00 to 23:00 are known
        self.assertTrue((published["price_p10"] == published["price_p90"]).all())
        self.assertEqual(published["price_p50"].iloc[0], 20.0)
        forecast = table[~table["price_published"]]
        self.assertEqual(forecast.index[0], pd.Timestamp("2026-11-08 23:00", tz="UTC"))
        self.assertTrue((forecast["price_p90"] - forecast["price_p10"] == 10).all())

    def test_keeps_serving_the_model_when_octopus_is_down(self):
        cutoff = live.latest_cutoff(NOW)
        with mock.patch.object(live, "model_snapshot", return_value=model_snapshot(cutoff)), \
                mock.patch.object(data, "fetch_agile_rates", side_effect=data.DataFetchError("down")):
            snapshot = live.current_forecast(NOW)
        self.assertEqual(snapshot.source, "model")
        self.assertFalse(snapshot.table["price_published"].any())

    def test_builds_once_per_cutoff_and_reuses_the_saved_copy(self):
        cutoff = live.latest_cutoff(NOW)
        built = model_snapshot(cutoff)

        def fake_build(c, now):
            live.LIVE_DIR.mkdir(parents=True, exist_ok=True)
            built.table.to_parquet(live.LIVE_DIR / "model_forecast.parquet")
            pd.DataFrame({"x": [1]}).to_parquet(live.LIVE_DIR / "model_features.parquet")
            (live.LIVE_DIR / "model_meta.json").write_text(json.dumps(
                {"cutoff": live.iso(c), "built_at": live.iso(now),
                 "calibration_sha256": live.calibration_fingerprint()}))
            return built

        self.use_real_model_snapshot()
        with mock.patch.object(live, "build_model_forecast", side_effect=fake_build) as build:
            first = live.model_snapshot(cutoff, NOW, background=False)
            second = live.model_snapshot(cutoff, NOW, background=False)
            self.assertEqual(build.call_count, 1)
            live.clear_memory()  # Like a server restart: the saved copy is used
            third = live.model_snapshot(cutoff, NOW, background=False)
            self.assertEqual(build.call_count, 1)
        self.assertIs(first, second)
        self.assertTrue(third.cached)
        self.assertEqual(third.data_as_of, cutoff)

    def test_a_failed_build_falls_back_to_published_data(self):
        self.use_real_model_snapshot()
        agile, carbon = self.published()
        with mock.patch.object(live, "build_model_forecast", side_effect=RuntimeError("boom")), agile, carbon:
            snapshot = live._refresh_forecast(NOW, background=False)
        self.assertEqual(snapshot.source, "published_only")


class ApiWiringTests(LiveTestCase):
    def setUp(self):
        super().setUp()
        from app import main
        self.main = main

    def test_forecast_endpoint_serves_live_data_with_freshness_fields(self):
        agile, carbon = self.published()
        with agile, carbon, mock.patch.object(live, "_utc_now", return_value=NOW):
            body = self.main.forecast()
        self.assertFalse(body["historical_demo"])
        self.assertEqual(body["source"], "published_only")
        self.assertEqual(body["slots"][0]["time"], "2026-11-08T10:00:00Z")
        self.assertTrue(all(slot["price_published"] for slot in body["slots"]))
        for key in ("data_as_of", "last_updated", "cached"):
            self.assertIn(key, body)

    def test_recommend_uses_live_data_and_the_simple_why(self):
        agile, carbon = self.published([30.0] * 20 + [5.0] * 6)  # Cheapest at the end
        request = self.main.RecommendRequest(
            appliance="washing_machine", earliest_start="2026-11-08T10:10:00Z",
            deadline="2026-11-08T23:00:00Z")
        with agile, carbon, mock.patch.object(live, "_utc_now", return_value=NOW):
            result = self.main.recommend(request)
        self.assertEqual(result["start"], "2026-11-08T20:00:00Z")
        self.assertEqual(result["source"], "published_only")
        self.assertFalse(result["historical_demo"])
        self.assertTrue(result["why"])

    def test_falls_back_to_the_historical_replay(self):
        with mock.patch.object(live, "current_forecast", return_value=None):
            body = self.main.forecast()
        self.assertTrue(body["historical_demo"])
        self.assertEqual(body["source"], "historical_demo")
        self.assertTrue(body["slots"])


if __name__ == "__main__":
    unittest.main()
