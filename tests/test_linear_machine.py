"""De drie overgangstabellen, en het slot op het verlaten van een poort."""

import unittest

from agency_os.linear import machine
from agency_os.linear.client import WriteRefused
from agency_os.linear.models import GateObservation


def observation(**overrides):
    base = dict(issue_id="issue-207", gate_state="Poort · Merge of publicatie",
                card_comment_id="c-card", card_created_at=None, outcome="akkoord",
                token="AKKOORD", source="comment", source_id="c-2", actor_id="user-mens",
                actor_name="Youp", actor_is_app=False, valid=True, refusal=None)
    base.update(overrides)
    return GateObservation(**base)


class TableTests(unittest.TestCase):
    def test_every_next_on_done_entry(self):
        expected = {
            ("WV", "Ingepland"): "In uitvoering",
            ("WV", "In uitvoering"): "Agentreview",
            ("WV", "Agentreview"): "QA op preview",
            ("WV", "QA op preview"): "Poort · Merge of publicatie",
            ("WV", "Na-merge controle"): "Klaar",
            ("KR", "Lead"): "Gekwalificeerd",
            ("KR", "Discovery"): "Voorstel",
            ("KR", "Voorstel"): "Poort 1 · Voorstel akkoord",
        }
        self.assertEqual(dict(machine.NEXT_ON_DONE), expected)
        for (team, state), target in expected.items():
            self.assertEqual(machine.next_state(team, state, "klaar"), target)

    def test_every_gate_on_approve_entry(self):
        expected = {
            ("WV", "Poort · Merge of publicatie"): "Na-merge controle",
            ("KR", "Poort 1 · Voorstel akkoord"): "Kickoff",
            ("KR", "Poort 2 · Oplevering akkoord"): "Poort 3 · Factuur akkoord",
            ("KR", "Poort 3 · Factuur akkoord"): "Afgerond",
        }
        self.assertEqual(dict(machine.GATE_ON_APPROVE), expected)
        for (team, state), target in expected.items():
            self.assertEqual(machine.next_state(team, state, "akkoord"), target)

    def test_every_gate_on_reject_entry(self):
        expected = {
            ("WV", "Poort · Merge of publicatie"): "In uitvoering",
            ("KR", "Poort 1 · Voorstel akkoord"): "Voorstel",
            ("KR", "Poort 2 · Oplevering akkoord"): "Klantacceptatie",
            ("KR", "Poort 3 · Factuur akkoord"): "Poort 2 · Oplevering akkoord",
        }
        self.assertEqual(dict(machine.GATE_ON_REJECT), expected)
        for (team, state), target in expected.items():
            self.assertEqual(machine.next_state(team, state, "afgekeurd"), target)

    def test_wait_state_per_team(self):
        self.assertEqual(machine.WAIT_STATE["WV"], "Wacht op input")
        self.assertEqual(machine.WAIT_STATE["KR"], "Wacht op input")
        self.assertEqual(machine.next_state("WV", "In uitvoering", "vraag"), "Wacht op input")

    def test_failure_outcomes_do_not_move_an_issue(self):
        for outcome in ("mislukt", "afgebroken", "onzin"):
            self.assertIsNone(machine.next_state("WV", "In uitvoering", outcome))

    def test_unknown_transition_is_none_and_not_a_guess(self):
        self.assertIsNone(machine.next_state("WV", "Binnen", "klaar"))
        self.assertIsNone(machine.next_state("KR", "Retainer", "klaar"))

    def test_is_gate(self):
        self.assertTrue(machine.is_gate("Poort · Merge of publicatie"))
        self.assertTrue(machine.is_gate("Poort 1 · Voorstel akkoord"))
        self.assertFalse(machine.is_gate("Klaar"))
        self.assertFalse(machine.is_gate(""))


class AssertMayLeaveTests(unittest.TestCase):
    def test_non_gate_states_need_no_observation(self):
        machine.assert_may_leave("In uitvoering", None)

    def test_gate_without_observation_is_refused(self):
        with self.assertRaises(WriteRefused):
            machine.assert_may_leave("Poort · Merge of publicatie", None)

    def test_gate_with_invalid_observation_is_refused_and_names_the_reason(self):
        with self.assertRaises(WriteRefused) as caught:
            machine.assert_may_leave(
                "Poort · Merge of publicatie",
                observation(valid=False, refusal="de actor is een app-account"))
        self.assertIn("app-account", str(caught.exception))

    def test_gate_without_a_decision_is_refused(self):
        with self.assertRaises(WriteRefused):
            machine.assert_may_leave("Poort · Merge of publicatie",
                                     observation(outcome=None, token=None))

    def test_valid_observation_passes(self):
        machine.assert_may_leave("Poort · Merge of publicatie", observation())
        machine.assert_may_leave("Poort · Merge of publicatie",
                                 observation(outcome="afgekeurd", token="AFGEKEURD"))


if __name__ == "__main__":
    unittest.main()
