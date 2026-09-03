"""De sqlite-store: het slot, het handelingenlogboek en de tellers."""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from agency_os.linear.models import MutationRecord
from agency_os.linear.store import SCHEMA_VERSION, Store, iso, parse_iso

from tests.support_linear import T0, make_run


class SchemaTests(unittest.TestCase):
    def test_it_creates_the_file_and_every_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "spil.sqlite3"
            store = Store(path)
            self.addCleanup(store.close)
            names = {row["name"] for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertLessEqual(
                {"meta", "claims", "runs", "mutations", "gate_events", "sessions",
                 "role_runs", "heartbeats"}, names)
            self.assertTrue(path.exists())
            self.assertEqual(store.get_meta("schema_version"), str(SCHEMA_VERSION))

    def test_migrating_twice_is_harmless(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.migrate()
        store.migrate()
        self.assertEqual(store.get_meta("schema_version"), str(SCHEMA_VERSION))
        self.assertLessEqual({"role", "model_key", "state"}, store._columns("sessions"))

    def test_foreign_keys_and_wal_are_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "spil.sqlite3")
            self.addCleanup(store.close)
            self.assertEqual(store.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(
                store.conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")


class MutationLogTests(unittest.TestCase):
    def test_the_store_is_a_mutation_sink(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.record(MutationRecord(
            at=T0, run_id="3f9a2c", mutation="issueUpdate", entity_id="issue-207",
            variables_digest="9f2c", variables_summary={"stateId": "<Agentreview>"},
            result_id="issue-207", ok=True, error=None, dry_run=False))
        rows = store.mutations_for("issue-207")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mutation"], "issueUpdate")
        self.assertEqual(rows[0]["at"], "2026-09-03T09:00:00Z")
        self.assertIn("Agentreview", rows[0]["variables_summary"])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_a_run_survives_the_database_unchanged(self):
        run = make_run()
        self.store.insert_run(run)
        self.assertEqual(self.store.runs_on(T0.date()), [run])

    def test_writing_the_same_run_id_twice_replaces_it(self):
        self.store.insert_run(make_run())
        self.store.insert_run(make_run(uitkomst="mislukt"))
        rows = self.store.runs_on(T0.date())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].uitkomst, "mislukt")

    def test_cost_per_issue_adds_up(self):
        self.store.insert_run(make_run(run_id="aaaaaa", kosten_eur=3.63))
        self.store.insert_run(make_run(run_id="bbbbbb", kosten_eur=1.20))
        self.assertAlmostEqual(self.store.cost_eur_for_issue("issue-207"), 4.83)

    def test_a_range_query_covers_several_days(self):
        self.store.insert_run(make_run(run_id="aaaaaa"))
        self.store.insert_run(make_run(run_id="bbbbbb", gestart=T0 + timedelta(days=2)))
        self.assertEqual(len(self.store.runs_between(T0.date(), T0.date())), 1)
        self.assertEqual(
            len(self.store.runs_between(T0.date(), (T0 + timedelta(days=2)).date())), 2)


class CounterTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_role_runs_count_per_issue_role_and_day(self):
        day = date(2026, 9, 3)
        self.assertEqual(self.store.role_run_count("issue-207", "redacteur", day), 0)
        self.assertEqual(self.store.bump_role_run("issue-207", "redacteur", day), 1)
        self.assertEqual(self.store.bump_role_run("issue-207", "redacteur", day), 2)
        self.assertEqual(self.store.bump_role_run("issue-207", "reviewer", day), 1)
        self.assertEqual(self.store.loops_on(day), 1)

    def test_a_session_row_is_upserted(self):
        self.store.upsert_session(issue_id="issue-207", run_id="3f9a2c", executor="native-codex",
                                  session_id=None, trigger_comment_id="c-1", triggered_at=T0)
        self.store.upsert_session(issue_id="issue-207", run_id="3f9a2c", executor="native-codex",
                                  session_id="sess-9", trigger_comment_id="c-1",
                                  triggered_at=T0, last_status="active", strikes=1)
        row = self.store.get_session("issue-207", "3f9a2c")
        self.assertEqual(row["session_id"], "sess-9")
        self.assertEqual(row["strikes"], 1)

    def test_heartbeats_are_kept_and_the_last_one_is_findable(self):
        self.store.record_heartbeat(at=T0, cycle=15, comment_id="c-1", runs_today=3,
                                    cost_eur_today=1.5, queue_len=2)
        self.store.record_heartbeat(at=T0 + timedelta(minutes=15), cycle=30, comment_id="c-2",
                                    runs_today=5, cost_eur_today=2.0, queue_len=1)
        self.assertEqual(self.store.last_heartbeat()["comment_id"], "c-2")

    def test_the_id_cache_survives_a_read(self):
        self.assertIsNone(self.store.cache_id("state", "Agentreview"))
        self.store.cache_id("state", "Agentreview", "uuid-1")
        self.assertEqual(self.store.cache_id("state", "Agentreview"), "uuid-1")


class TimeHelperTests(unittest.TestCase):
    def test_iso_round_trip(self):
        self.assertEqual(iso(T0), "2026-09-03T09:00:00Z")
        self.assertEqual(parse_iso("2026-09-03T09:00:00Z"), T0)
        self.assertIsNone(iso(None))
        self.assertIsNone(parse_iso(None))
        self.assertIsNone(parse_iso(""))


if __name__ == "__main__":
    unittest.main()
