"""Verbruik naar geld: gemeten bedragen winnen, onbekend blijft onbekend."""

from __future__ import annotations

import unittest

from agency_os.executors.base import Usage
from agency_os.executors.cost import budget_flag, normalise, to_eur
from tests.executor_fakes import FakeFxRate, FakePriceRow

PRICES = (
    FakePriceRow("claude-sonnet-5", 3.0, 15.0, 0.3),
    FakePriceRow("claude-opus-5", 15.0, 75.0, 1.5),
)


class NormaliseTests(unittest.TestCase):
    def test_a_reported_amount_beats_the_price_table(self):
        usage = Usage(tokens_in=1_000_000, cost_usd=0.42, source="claude-json")
        self.assertEqual(normalise(usage, "claude-sonnet-5", PRICES).cost_usd, 0.42)

    def test_computes_the_amount_from_tokens_and_list_price(self):
        usage = Usage(tokens_in=1_000_000, tokens_out=100_000, cache_read=2_000_000,
                      source="codex-cli")
        result = normalise(usage, "claude-sonnet-5", PRICES)
        self.assertAlmostEqual(result.cost_usd, 3.0 + 1.5 + 0.6)
        self.assertTrue(result.metered)

    def test_a_model_without_a_price_row_becomes_unmetered(self):
        usage = Usage(tokens_in=1000, source="codex-cli")
        result = normalise(usage, "grok-4.6", PRICES)
        self.assertFalse(result.metered)
        self.assertEqual(result.cost_usd, 0.0)

    def test_the_model_name_is_matched_case_insensitively(self):
        usage = Usage(tokens_in=1_000_000, source="codex-cli")
        self.assertAlmostEqual(normalise(usage, "Claude-Sonnet-5", PRICES).cost_usd, 3.0)

    def test_an_unmetered_lane_stays_at_zero(self):
        usage = Usage(tokens_in=999, source="native-unmetered", metered=False)
        result = normalise(usage, "claude-sonnet-5", PRICES)
        self.assertEqual(result.cost_usd, 0.0)
        self.assertFalse(result.metered)

    def test_a_run_without_any_measurement_is_not_priced(self):
        result = normalise(Usage(source="unknown"), "claude-sonnet-5", PRICES)
        self.assertFalse(result.metered)
        self.assertEqual(result.cost_usd, 0.0)


class CurrencyTests(unittest.TestCase):
    def test_uses_the_configured_rate_and_never_a_hardcoded_one(self):
        self.assertAlmostEqual(to_eur(10.0, FakeFxRate(usd_eur=0.92)), 9.2)
        self.assertAlmostEqual(to_eur(10.0, FakeFxRate(usd_eur=0.85)), 8.5)

    def test_the_budget_flag_trips_at_the_threshold(self):
        self.assertFalse(budget_flag(9.99))
        self.assertTrue(budget_flag(10.0))
        self.assertTrue(budget_flag(3.0, threshold_eur=2.5))


if __name__ == "__main__":
    unittest.main()
