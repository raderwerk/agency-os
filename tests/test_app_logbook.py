"""Het logboek: één regel per gebeurtenis, per dag, alleen toevoegen."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tests.fakes import FakeClient  # noqa: F401  (installeert de contract-invullers)

from agency_os.app.logbook import Logbook
from agency_os.linear.models import MutationRecord


def a_mutation(**overrides) -> MutationRecord:
    defaults = dict(
        at=datetime(2026, 9, 3, 11, 14, 52, tzinfo=timezone.utc),
        run_id="3f9a2c",
        mutation="issueUpdate",
        entity_id="490eb350-0000",
        variables_digest="9f2c",
        variables_summary={"stateId": "Agentreview", "addedLabelIds": ["run/klaar"]},
        result_id="490eb350-0000",
        ok=True,
        error=None,
        dry_run=False,
    )
    defaults.update(overrides)
    return MutationRecord(**defaults)


class LogbookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.book = Logbook(Path(self.tmp.name) / "logbook")

    def lines(self, day: date) -> list[dict]:
        path = self.book.path_for(day)
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_a_mutation_lands_on_the_day_of_the_mutation(self):
        self.book.record(a_mutation())

        lines = self.lines(date(2026, 9, 3))
        self.assertEqual(1, len(lines))
        self.assertEqual("2026-09-03T11:14:52Z", lines[0]["at"])
        self.assertEqual("mutation", lines[0]["kind"])
        self.assertEqual("3f9a2c", lines[0]["run_id"])
        self.assertEqual("issueUpdate", lines[0]["payload"]["mutation"])
        self.assertEqual(["run/klaar"], lines[0]["payload"]["variables_summary"]["addedLabelIds"])

    def test_events_append_and_stay_one_line_each(self):
        for index in range(3):
            self.book.write("route", run_id="3f9a2c", issue="WV-207", payload={"nummer": index})

        today = datetime.now(timezone.utc).date()
        self.assertEqual([0, 1, 2], [line["payload"]["nummer"] for line in self.lines(today)])

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            self.book.write("verzonnen", run_id=None, issue=None, payload={})

    def test_export_walks_the_days_and_skips_the_empty_ones(self):
        self.book.record(a_mutation())
        self.book.record(a_mutation(at=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc)))

        export = self.book.export(date(2026, 9, 3), date(2026, 9, 5))
        self.assertEqual(2, len(export.splitlines()))
        self.assertEqual("", self.book.export(date(2026, 9, 6), date(2026, 9, 7)))

    def test_a_naive_timestamp_is_read_as_utc(self):
        self.book.record(a_mutation(at=datetime(2026, 9, 3, 11, 14, 52)))
        self.assertEqual("2026-09-03T11:14:52Z", self.lines(date(2026, 9, 3))[0]["at"])

    def test_it_survives_a_payload_that_is_not_plain_json(self):
        self.book.write("run", run_id="3f9a2c", issue="WV-207", payload={"duur": timedelta(seconds=3)})
        self.assertIn("0:00:03", self.book.export(datetime.now(timezone.utc).date(), datetime.now(timezone.utc).date()))


if __name__ == "__main__":
    unittest.main()
