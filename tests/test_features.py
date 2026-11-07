"""Regression checks for publication cutoffs and UK clock changes."""

import unittest

import numpy as np
import pandas as pd

import config
from src import features
from src.build_dataset import CARBON, DEMAND, PRICE


def example_history():
    slots = pd.date_range("2026-03-20", "2026-04-01", freq="30min", tz="UTC")
    rows = {
        "time_utc": slots,
        PRICE: np.arange(len(slots), dtype=float),
        CARBON: 100.0 + np.arange(len(slots)),
        DEMAND: 20_000.0 + np.arange(len(slots)),
        "demand_published_at_utc": slots + features.SLOT,
        "gb_carbon_actual_suspect": False,
    }
    for lead in config.WEATHER_LEAD_DAYS:
        rows[f"weather_day{lead}_assumed_available_at_utc"] = (
            slots.floor("h") - pd.Timedelta(days=lead)
            + pd.Timedelta(hours=config.WEATHER_RELEASE_BUFFER_HOURS)
        )
        for column in features.WEATHER_COLUMNS:
            rows[f"{column}_previous_day{lead}"] = float(4 + lead)
    return pd.DataFrame(rows)


class FeatureAvailabilityTests(unittest.TestCase):
    cutoff = pd.Timestamp("2026-03-28T15:00Z")

    def test_post_cutoff_values_cannot_change_features(self):
        history = example_history()
        changed = history.copy()
        end = history["time_utc"] + features.SLOT
        carbon_buffer = pd.Timedelta(hours=config.CARBON_RELEASE_BUFFER_HOURS)
        changed.loc[end.gt(self.cutoff), PRICE] += 1_000
        changed.loc[(end + carbon_buffer).gt(self.cutoff), CARBON] += 1_000
        changed.loc[history["demand_published_at_utc"].gt(self.cutoff), DEMAND] += 1_000
        for lead in config.WEATHER_LEAD_DAYS:
            late = history[f"weather_day{lead}_assumed_available_at_utc"].gt(self.cutoff)
            columns = [f"{name}_previous_day{lead}" for name in features.WEATHER_COLUMNS]
            changed.loc[late, columns] += 1_000
        changed["gb_carbon_forecast_archive_gco2_kwh"] = 99_999
        changed["era5_south_scotland_wind_speed_100m"] = 99_999

        before = features.build_features(history, [self.cutoff], assume_availability=True)
        after = features.build_features(changed, [self.cutoff], assume_availability=True)
        columns = list(features.FEATURE_COLUMNS)
        pd.testing.assert_frame_equal(before[columns], after[columns])
        self.assertNotEqual(before["label_price_p_kwh"].iloc[0], after["label_price_p_kwh"].iloc[0])
        self.assertEqual(len(before), 96)
        self.assertTrue(before["weather_available_at_utc"].le(self.cutoff).all())
        self.assertTrue(pd.isna(before.loc[before["horizon_minutes"] == 1440, "price_yesterday"].iloc[0]))

    def test_a_late_recorded_timestamp_overrides_the_buffer(self):
        history = example_history()
        late = self.cutoff + features.SLOT
        history["gb_carbon_actual_published_at_utc"] = pd.NaT
        history["gb_carbon_actual_published_at_utc"] = history["gb_carbon_actual_published_at_utc"].dt.tz_localize("UTC")
        week_ago = history["time_utc"].eq(self.cutoff - pd.Timedelta(days=7))
        history.loc[week_ago, "gb_carbon_actual_published_at_utc"] = late
        history["weather_day1_published_at_utc"] = late

        buffered = features.build_features(history, [self.cutoff], horizon_hours=1, assume_availability=True)
        recorded = features.build_features(history, [self.cutoff], horizon_hours=1)
        self.assertTrue(pd.isna(buffered["carbon_last_week"].iloc[0]))
        self.assertEqual(buffered["weather_lead_hours"].iloc[0], 48)
        self.assertTrue(recorded[list(features.WEATHER_COLUMNS)].isna().all().all())
        self.assertTrue(recorded["carbon_mean_24h"].isna().all())

    def test_local_lags_handle_both_clock_changes(self):
        targets = pd.to_datetime(["2026-03-30T00:30Z", "2026-10-26T01:30Z"], utc=True)
        earlier = features._local_lag(targets, 1)
        self.assertTrue(pd.isna(earlier[0]))
        self.assertEqual(earlier[1], pd.Timestamp("2026-10-25T01:30Z"))

    def test_one_off_holiday_needs_a_public_announcement(self):
        calendar = pd.read_csv(config.SCOTLAND_BANK_HOLIDAYS)
        local = pd.DatetimeIndex(["2026-06-15", "2026-06-15", "2029-01-01"])
        cutoffs = pd.to_datetime(["2026-02-05T10:30Z", "2026-02-05T11:00Z", "2028-12-31T15:00Z"], utc=True)
        flags = features._bank_holiday_flags(local, cutoffs, calendar)
        self.assertEqual(list(flags[:2]), [0, 1])
        self.assertTrue(pd.isna(flags[2]))
