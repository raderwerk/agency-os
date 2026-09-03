"""De commandoregel: argumenten, afloopcodes en het logboek-commando."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests.fakes import FakeClient  # noqa: F401  (installeert de contract-invullers)

from agency_os.app import cli

ENV = {
    "SPIL_LINEAR_API_KEY": "lin_api_test",
    "SPIL_DISPATCHER_USER_ID": "user-spil",
    "SPIL_APPROVER_IDS": "user-mens",
    "SPIL_FX_USD_EUR": "0.86",
    "SPIL_FX_SOURCE": "ECB",
    "SPIL_FX_DATE": "2026-09-02",
}


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        env_file = Path(self.tmp.name) / "spil.env"
        env_file.write_text("\n".join(f"{k}={v}" for k, v in ENV.items()), encoding="utf-8")
        self.env = {"SPIL_CONFIG_FILE": str(env_file), "SPIL_STATE_DIR": str(self.state)}

    def run_cli(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_a_broken_config_exits_with_two_and_says_what_is_missing(self):
        with mock.patch.dict(os.environ, {"SPIL_CONFIG_FILE": "/bestaat/niet.env"}, clear=True):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["status"])
        self.assertEqual(cli.CONFIG_ERROR, code)
        self.assertIn("SPIL_LINEAR_API_KEY", out.getvalue())

    def test_run_needs_once_or_loop(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.main(["run"])
        self.assertNotEqual(0, caught.exception.code)

    def test_the_logbook_command_prints_the_lines_of_the_day(self):
        from agency_os.app.logbook import Logbook

        today = datetime.now(timezone.utc).date()
        Logbook(self.state / "logbook").write("skip", run_id=None, issue="WV-207", payload={"reden": "buiten-mvp"})

        code, printed = self.run_cli("ledger", "--logbook", "--since", today.isoformat(), "--until", today.isoformat())

        self.assertEqual(cli.OK, code)
        self.assertIn("buiten-mvp", printed)

    def test_a_bad_date_is_a_config_error(self):
        code, printed = self.run_cli("ledger", "--logbook", "--since", "gisteren")
        self.assertEqual(cli.CONFIG_ERROR, code)
        self.assertIn("JJJJ-MM-DD", printed)

    def test_command_line_flags_beat_the_config_file(self):
        parsed = cli._parser().parse_args(["run", "--once", "--interval", "5", "--max-claims", "1"])
        self.assertEqual({"SPIL_INTERVAL_S": "5", "SPIL_MAX_CLAIMS_PER_CYCLE": "1"}, cli._overrides(parsed))


if __name__ == "__main__":
    unittest.main()
