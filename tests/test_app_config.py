"""Configuratie: volgorde van winnen, wat fataal is, en wat nooit geprint wordt."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import FakeClient  # noqa: F401  (installeert de contract-invullers)

from agency_os.app.config import Config, ConfigError

REQUIRED = {
    "SPIL_LINEAR_API_KEY": "lin_api_geheim",
    "SPIL_DISPATCHER_USER_ID": "user-spil",
    "SPIL_APPROVER_IDS": "user-mens,user-collega",
    "SPIL_FX_USD_EUR": "0.86",
    "SPIL_FX_SOURCE": "ECB",
    "SPIL_FX_DATE": "2026-09-02",
}


class ConfigTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_file = Path(self.tmp.name) / "spil.env"
        self.state_dir = Path(self.tmp.name) / "state"

    def write_file(self, **values) -> None:
        lines = ["# spil.env", ""] + [f"{key}={value}" for key, value in values.items()]
        self.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def load(self, **env) -> Config:
        environment = {"SPIL_CONFIG_FILE": str(self.env_file), "SPIL_STATE_DIR": str(self.state_dir), **env}
        with mock.patch.dict(os.environ, environment, clear=True):
            return Config.load()


class PrecedenceTest(ConfigTestCase):
    def test_env_beats_file_beats_default(self):
        self.write_file(**REQUIRED, SPIL_INTERVAL_S="30", SPIL_PANEL_ISSUE="WV-999")
        cfg = self.load(SPIL_INTERVAL_S="10")

        self.assertEqual(10, cfg.interval_s, "env wint van bestand")
        self.assertEqual("WV-999", cfg.panel_identifier, "bestand wint van standaard")
        self.assertEqual(4, cfg.max_claims_per_cycle, "standaard vult de rest aan")

    def test_argv_overrides_beat_everything(self):
        self.write_file(**REQUIRED)
        with mock.patch.dict(os.environ, {"SPIL_CONFIG_FILE": str(self.env_file),
                                          "SPIL_STATE_DIR": str(self.state_dir),
                                          "SPIL_INTERVAL_S": "10"}, clear=True):
            cfg = Config.load({"SPIL_INTERVAL_S": "5"})
        self.assertEqual(5, cfg.interval_s)

    def test_key_source_names_where_the_key_came_from(self):
        self.write_file(**REQUIRED)
        self.assertEqual(f"file:{self.env_file}", self.load().linear_api_key_source)
        self.assertEqual(
            "env:SPIL_LINEAR_API_KEY",
            self.load(SPIL_LINEAR_API_KEY="lin_api_anders").linear_api_key_source,
        )

    def test_derived_paths_hang_under_the_state_dir(self):
        self.write_file(**REQUIRED)
        cfg = self.load()
        self.assertEqual(self.state_dir / "spil.sqlite3", cfg.db_path)
        self.assertEqual(self.state_dir / "logbook", cfg.logbook_dir)


class FatalTest(ConfigTestCase):
    def test_missing_api_key_is_fatal(self):
        self.write_file(**{k: v for k, v in REQUIRED.items() if k != "SPIL_LINEAR_API_KEY"})
        with self.assertRaises(ConfigError) as caught:
            self.load()
        self.assertIn("SPIL_LINEAR_API_KEY", str(caught.exception))

    def test_missing_fx_is_fatal(self):
        for missing in ("SPIL_FX_USD_EUR", "SPIL_FX_SOURCE", "SPIL_FX_DATE"):
            with self.subTest(missing=missing):
                self.write_file(**{k: v for k, v in REQUIRED.items() if k != missing})
                with self.assertRaises(ConfigError) as caught:
                    self.load()
                self.assertIn(missing, str(caught.exception))

    def test_no_approvers_is_fatal(self):
        self.write_file(**{**REQUIRED, "SPIL_APPROVER_IDS": ","})
        with self.assertRaises(ConfigError):
            self.load()

    def test_worktree_root_under_a_forbidden_prefix_is_fatal(self):
        self.write_file(**REQUIRED)
        with self.assertRaises(ConfigError) as caught:
            self.load(SPIL_WORKTREE_ROOT="/Users/youp/Developer/Fightclub/TowMotive/.worktrees")
        self.assertIn("verboden", str(caught.exception))

    def test_budget_must_be_three_rising_numbers(self):
        self.write_file(**REQUIRED)
        for bad in ("200,220", "225,220,200", "veel,220,225"):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigError):
                    self.load(SPIL_ISSUE_BUDGET=bad)

    def test_prices_need_four_fields(self):
        self.write_file(**REQUIRED)
        with self.assertRaises(ConfigError):
            self.load(SPIL_PRICES="claude-opus-5:5:25")


class ValuesTest(ConfigTestCase):
    def test_redacted_never_contains_the_key(self):
        self.write_file(**REQUIRED)
        cfg = self.load()
        redacted = cfg.redacted()
        self.assertNotIn("lin_api_geheim", repr(redacted))
        self.assertEqual(f"file:{self.env_file}", redacted["linear_api_key_source"])
        self.assertNotIn("linear_api_key", redacted)

    def test_fx_and_prices_are_parsed(self):
        self.write_file(**REQUIRED)
        cfg = self.load(SPIL_PRICES="claude-opus-5:5:25:0.5, claude-sonnet-5:2:10:0.2")
        self.assertEqual(0.86, cfg.fx.usd_eur)
        self.assertEqual("ECB", cfg.fx.source)
        self.assertEqual(("claude-opus-5", "claude-sonnet-5"), tuple(p.model for p in cfg.prices))
        self.assertEqual(25.0, cfg.prices[0].usd_out_per_mtok)

    def test_default_prices_cover_every_model_the_router_can_pick(self):
        from agency_os.app.routing import MODELS

        self.write_file(**REQUIRED)
        priced = {row.model for row in self.load().prices}
        for model in MODELS.values():
            self.assertIn(model.ledger, priced, f"{model.key} staat niet in de prijskaart")

    def test_dry_run_pulls_the_executors_along(self):
        self.write_file(**REQUIRED)
        cfg = self.load()
        self.assertFalse(cfg.executors.dry_run)
        self.assertTrue(cfg.with_overrides(dry_run=True).executors.dry_run)

    def test_booleans_and_budget(self):
        self.write_file(**REQUIRED)
        self.assertFalse(self.load().allow_fable)
        self.assertTrue(self.load(SPIL_ALLOW_FABLE="true").allow_fable)
        self.assertEqual((200, 220, 225), self.load().issue_budget)


if __name__ == "__main__":
    unittest.main()
