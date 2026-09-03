"""Hartslag en wachthond: wanneer hij slaat, en wat de wachthond precies doet."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.fakes import FakeClient, make_issue, temp_store

from agency_os.app import heartbeat
from agency_os.app.heartbeat import ALIVE, DEAD_LABEL, TRIPPED, UNKNOWN
from agency_os.linear.client import LinearError

DISPATCHER = "user-spil"


class Cfg:
    """Alleen de velden die de hartslag en de wachthond lezen."""

    panel_identifier = "WV-156"
    watchdog_max_age_s = 1800
    issue_budget = (200, 220, 225)


class DueTest(unittest.TestCase):
    def test_every_fifteenth_cycle(self):
        due = [cycle for cycle in range(1, 46) if heartbeat.due(cycle, 15)]
        self.assertEqual([15, 30, 45], due)

    def test_cycle_zero_and_a_disabled_interval_never_fire(self):
        self.assertFalse(heartbeat.due(0, 15))
        self.assertFalse(heartbeat.due(15, 0))


class BeatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = temp_store(self.tmp.name)
        self.client = FakeClient(dispatcher_user_id=DISPATCHER)
        self.panel = self.client.issue("WV-156")

    def test_it_writes_one_comment_and_records_the_beat(self):
        self.store.set_meta("queue_len", "3")
        self.store.set_meta("cycle", "15")

        heartbeat.beat(self.client, self.store, Cfg(), self.panel, run_id="a1b2c3")

        comments = self.client.comments(self.panel.id)
        self.assertEqual(1, len(comments))
        self.assertTrue(comments[0].body.startswith("**Spil · dispatcher · run a1b2c3"))
        self.assertIn("3 issues in de wachtrij", comments[0].body)
        self.assertIn("noodstop bij 225", comments[0].body, "de grens komt uit de config, niet uit de code")
        self.assertIsNotNone(self.store.last_heartbeat_at())

    def test_it_refreshes_the_counters_between_the_markers(self):
        heartbeat.beat(self.client, self.store, Cfg(), self.panel, run_id="a1b2c3")

        description = self.client.issue("WV-156").description
        self.assertIn("<!-- spil:tellers -->", description)
        self.assertIn("Laatste hartslag:", description)
        self.assertNotIn("nog geen hartslag", description)
        self.assertIn("De schakelaars van de werkplaats.", description, "tekst van een mens blijft staan")

    def test_a_panel_without_markers_keeps_its_description(self):
        panel = make_issue(id="panel-2", identifier="WV-156", description="Vrije tekst", contract=None)
        client = FakeClient([panel], dispatcher_user_id=DISPATCHER)

        heartbeat.beat(client, self.store, Cfg(), panel, run_id="a1b2c3")

        self.assertEqual("Vrije tekst", client.issue("WV-156").description)
        self.assertEqual(["commentCreate"], [m.mutation for m in client.mutations])


class WatchdogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = temp_store(self.tmp.name)
        self.client = FakeClient(dispatcher_user_id=DISPATCHER)

    def beat_ago(self, minutes: int) -> None:
        self.store.record_heartbeat(
            at=datetime.now(timezone.utc) - timedelta(minutes=minutes),
            cycle=1, comment_id="c-1", runs_today=0, cost_eur_today=0.0, queue_len=0,
        )

    def test_a_fresh_heartbeat_means_alive_and_no_writes(self):
        self.beat_ago(5)
        self.assertEqual(ALIVE, heartbeat.watchdog(self.client, self.store, Cfg()))
        self.assertEqual([], self.client.mutations)

    def test_an_old_heartbeat_trips_the_stop_with_exactly_one_comment(self):
        self.beat_ago(45)

        self.assertEqual(TRIPPED, heartbeat.watchdog(self.client, self.store, Cfg()))

        panel = self.client.issue("WV-156")
        self.assertIn(DEAD_LABEL, panel.labels)
        comments = self.client.comments(panel.id)
        self.assertEqual(1, len(comments))
        self.assertIn("motor-dood", comments[0].body)
        self.assertEqual(
            ["issueUpdate", "commentCreate"], [m.mutation for m in self.client.mutations],
            "één label erbij en één comment; meer doet de wachthond nooit",
        )

    def test_it_never_writes_twice(self):
        self.beat_ago(45)
        heartbeat.watchdog(self.client, self.store, Cfg())
        before = len(self.client.mutations)

        self.assertEqual(TRIPPED, heartbeat.watchdog(self.client, self.store, Cfg()))
        self.assertEqual(before, len(self.client.mutations))

    def test_a_401_counts_as_death(self):
        self.beat_ago(1)
        with mock.patch.object(self.client, "issue", side_effect=LinearError("401 authentication required")):
            self.assertEqual(TRIPPED, heartbeat.watchdog(self.client, self.store, Cfg()))
        self.assertEqual([], self.client.mutations, "met een dode sleutel valt er niets te schrijven")

    def test_an_unreachable_api_is_not_death(self):
        self.beat_ago(1)
        with mock.patch.object(self.client, "issue", side_effect=LinearError("503 service unavailable")):
            self.assertEqual(UNKNOWN, heartbeat.watchdog(self.client, self.store, Cfg()))
        with mock.patch.object(self.client, "issue", side_effect=TimeoutError("dns")):
            self.assertEqual(UNKNOWN, heartbeat.watchdog(self.client, self.store, Cfg()))

    def test_without_any_heartbeat_the_watchdog_says_it_cannot_tell(self):
        self.assertEqual(UNKNOWN, heartbeat.watchdog(self.client, self.store, Cfg()))
        self.assertEqual([], self.client.mutations)


if __name__ == "__main__":
    unittest.main()
