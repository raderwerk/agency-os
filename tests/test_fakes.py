"""De gedeelde testdubbels zelf: A en B bouwen hierop, dus dit moet kloppen."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date

from tests.fakes import FakeClient, canonical_label, make_issue, make_run, make_session, temp_store

from agency_os.gate import InvalidGateToken
from agency_os.linear.client import WriteRefused


class CanonicalLabelTest(unittest.TestCase):
    def test_leaf_plus_parent(self):
        self.assertEqual("soort/contentstuk", canonical_label({"name": "contentstuk", "parent": {"name": "soort"}}))
        self.assertEqual("risico-publiek", canonical_label({"name": "risico-publiek", "parent": None}))
        self.assertEqual(
            "repo/raderwerk/raderwerk-content",
            canonical_label({"name": "raderwerk/raderwerk-content", "parent": {"name": "repo"}}),
            "een repolabel heeft twee schuine strepen",
        )

    def test_the_default_issue_is_wv_207_shaped(self):
        issue = make_issue()
        self.assertEqual("WV-207", issue.identifier)
        self.assertEqual("contentstuk", issue.soort)
        self.assertEqual("sonnet", issue.agent_hint)
        self.assertEqual("raderwerk/raderwerk-content", issue.repo)
        self.assertEqual("laag", issue.risico, "geen risicolabel betekent laag")
        self.assertEqual(frozenset({"risico-publiek"}), issue.flags)
        self.assertFalse(issue.is_gate_state)


class GuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeClient()
        self.issue = self.client.issue("WV-207")

    def test_the_fake_refuses_to_open_a_gate_with_a_comment(self):
        with self.assertRaises(InvalidGateToken):
            self.client.create_comment(self.issue.id, "AKKOORD\n\nnamens de mens", run_id="a1b2c3")

    def test_the_fake_refuses_a_gate_label(self):
        for label in ("poort/akkoord", "poort/afgekeurd"):
            with self.subTest(label=label), self.assertRaises(WriteRefused):
                self.client.update_issue(self.issue.id, run_id="a1b2c3", added_labels=[label])

    def test_the_fake_refuses_to_clear_the_emergency_stop(self):
        with self.assertRaises(WriteRefused):
            self.client.update_issue(self.issue.id, run_id="a1b2c3", removed_labels=["schakelaar/pauze-alles"])

    def test_leaving_a_gate_state_needs_a_gate_observation(self):
        gate_issue = self.client.issue("WV-208")
        with self.assertRaises(WriteRefused):
            self.client.update_issue(gate_issue.id, run_id="a1b2c3", state="Na-merge controle")
        self.client.update_issue(gate_issue.id, run_id="a1b2c3", state="Na-merge controle", gate_ok=True)
        self.assertEqual("Na-merge controle", self.client.issue("WV-208").state_name)


class RecordingTest(unittest.TestCase):
    def test_every_write_becomes_a_mutation_record(self):
        client = FakeClient()
        issue = client.issue("WV-207")
        client.update_issue(issue.id, run_id="a1b2c3", state="In uitvoering", added_labels=["run/bezet"])

        record = client.mutations[-1]
        self.assertEqual("issueUpdate", record.mutation)
        self.assertEqual("a1b2c3", record.run_id)
        self.assertEqual(["run/bezet"], record.variables_summary["addedLabelIds"])
        self.assertFalse(record.dry_run)
        self.assertEqual(64, len(record.variables_digest))

    def test_dry_run_records_but_does_not_change_anything(self):
        client = FakeClient(dry_run=True)
        issue = client.issue("WV-207")
        client.update_issue(issue.id, run_id="a1b2c3", state="In uitvoering")

        self.assertEqual("Ingepland", client.issue("WV-207").state_name)
        self.assertTrue(client.mutations[-1].dry_run)
        self.assertIsNone(client.mutations[-1].result_id)

    def test_fail_next_injects_one_failure(self):
        client = FakeClient()
        client.fail_next("issueUpdate", RuntimeError("429"))
        with self.assertRaises(RuntimeError):
            client.update_issue(client.issue("WV-207").id, run_id="a1b2c3", priority=1)
        client.update_issue(client.issue("WV-207").id, run_id="a1b2c3", priority=1)


class BuildersTest(unittest.TestCase):
    def test_make_run_and_make_session_have_usable_defaults(self):
        self.assertEqual("klaar", make_run().uitkomst)
        self.assertEqual("mislukt", make_run(uitkomst="mislukt").uitkomst)
        self.assertEqual("complete", make_session().status)
        self.assertEqual("Codex", make_session().app_user_name)

    def test_temp_store_is_a_real_sqlite_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = temp_store(tmp)
            store.bump_role_run("issue-1", "redacteur", date(2026, 9, 3))
            self.assertEqual(1, store.role_run_count("issue-1", "redacteur", date(2026, 9, 3)))
            self.assertTrue(store.path.exists())


if __name__ == "__main__":
    unittest.main()
