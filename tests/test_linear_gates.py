"""De poort: vijf voorwaarden, twee kanalen, zes handelingen, geen derde poging."""

import unittest
from datetime import timedelta

from agency_os.linear import gates
from agency_os.linear.client import WriteRefused
from agency_os.linear.store import Store

from tests.support_linear import (
    APPROVER,
    DISPATCHER,
    T0,
    FakeLinearClient,
    make_comment,
    make_issue,
)

APPROVERS = frozenset({APPROVER})
GATE = "Poort · Merge of publicatie"


def gate_issue(**overrides):
    base = dict(state_name=GATE, state_type="started",
                labels=("dienst/web", "klant/zoutkaap", "poort/wacht-op-mens", "run/bezet"))
    base.update(overrides)
    return make_issue(**base)


def card(**overrides):
    base = dict(id="c-card", body="**Poortkaart merge · WV-207**\n\nAntwoord met AKKOORD.",
                created_at=T0, author_id=DISPATCHER, author_name="Spil", author_is_app=True)
    base.update(overrides)
    return make_comment(**base)


def decision(body="AKKOORD\n\nZiet er goed uit.", **overrides):
    base = dict(id="c-2", body=body, created_at=T0 + timedelta(minutes=20),
                author_id=APPROVER, author_name="Youp", author_is_app=False)
    base.update(overrides)
    return make_comment(**base)


def observe(issue, comment_list, **client_kwargs):
    client = FakeLinearClient([issue], comments={issue.id: comment_list}, **client_kwargs)
    return client, gates.evaluate_gate(client, issue, approver_ids=APPROVERS,
                                       dispatcher_user_id=DISPATCHER)


class CommentChannelTests(unittest.TestCase):
    def test_a_clean_akkoord_from_an_approver_is_valid(self):
        _, obs = observe(gate_issue(), [card(), decision()])
        self.assertTrue(obs.valid)
        self.assertEqual(obs.outcome, "akkoord")
        self.assertEqual(obs.token, "AKKOORD")
        self.assertEqual(obs.source, "comment")
        self.assertEqual(obs.source_id, "c-2")
        self.assertIsNone(obs.refusal)

    def test_condition_1_actor_not_on_the_approver_list(self):
        _, obs = observe(gate_issue(), [card(), decision(author_id="user-vreemd")])
        self.assertFalse(obs.valid)
        self.assertEqual(obs.refusal, gates.REFUSALS[1])

    def test_condition_2_actor_is_an_app(self):
        _, obs = observe(gate_issue(), [card(), decision(author_is_app=True)])
        self.assertFalse(obs.valid)
        self.assertEqual(obs.refusal, gates.REFUSALS[2])

    def test_condition_3_actor_is_the_dispatcher_itself(self):
        client = FakeLinearClient(
            [gate_issue()],
            comments={"issue-207": [card(), decision(author_id=DISPATCHER)]})
        obs = gates.evaluate_gate(client, gate_issue(),
                                  approver_ids=frozenset({DISPATCHER}),
                                  dispatcher_user_id=DISPATCHER)
        self.assertFalse(obs.valid)
        self.assertEqual(obs.refusal, gates.REFUSALS[3])

    def test_condition_4_a_decision_older_than_the_card_is_not_read_at_all(self):
        """Voorwaarde 4 in het tekstkanaal: alleen wat ná de kaart staat telt.

        Het besluit van een vorige poortronde mag geen antwoord op deze kaart
        worden -- niet als geldig besluit en ook niet als weigering, want dan
        loopt elk issue dat een poort twee keer bezoekt vast.
        """
        _, obs = observe(gate_issue(),
                         [card(), decision(created_at=T0 - timedelta(minutes=5))])
        self.assertIsNone(obs.outcome)
        self.assertIsNone(obs.refusal)
        self.assertFalse(obs.valid)

    def test_the_newest_decision_after_the_card_wins(self):
        """Ronde 2: een verse kaart met een vers AKKOORD na een oude afkeuring."""
        old_card = card(id="c-card-1", created_at=T0 - timedelta(days=1))
        old_reject = decision(id="c-oud", body="AFGEKEURD: te vaag",
                              created_at=T0 - timedelta(days=1) + timedelta(minutes=10))
        _, obs = observe(gate_issue(), [old_card, old_reject, card(), decision()])
        self.assertTrue(obs.valid, obs.refusal)
        self.assertEqual(obs.outcome, "akkoord")
        self.assertEqual(obs.source_id, "c-2")

    def test_a_decision_without_a_card_is_refused_and_never_acted_on(self):
        _, obs = observe(gate_issue(), [decision()])
        self.assertFalse(obs.valid)
        self.assertEqual(obs.outcome, "akkoord")
        self.assertEqual(obs.refusal, gates.REFUSALS[6])

    def test_afgekeurd_with_the_reason_on_the_same_line_is_the_form_the_card_asks_for(self):
        _, obs = observe(gate_issue(),
                         [card(), decision(body="AFGEKEURD: de tekst is te vaag")])
        self.assertTrue(obs.valid, obs.refusal)
        self.assertEqual(obs.outcome, "afgekeurd")
        self.assertEqual(obs.reason, "de tekst is te vaag")

    def test_an_unreadable_token_carries_an_outcome_so_the_human_gets_an_answer(self):
        _, obs = observe(gate_issue(), [card(), decision(body="AKKOORD, ziet er goed uit")])
        self.assertEqual(obs.outcome, gates.UNREADABLE)
        self.assertFalse(obs.valid)

    def test_condition_5_the_first_line_is_not_an_exact_token(self):
        _, obs = observe(gate_issue(), [card(), decision(body="AKKOORD, ziet er goed uit")])
        self.assertFalse(obs.valid)
        self.assertTrue(obs.refusal.startswith(gates.REFUSALS[5]))

    def test_a_quoted_token_never_counts(self):
        _, obs = observe(gate_issue(), [card(), decision(body="> AKKOORD\n\nzei de klant")])
        self.assertIsNone(obs.outcome)
        self.assertFalse(obs.valid)

    def test_a_fenced_token_never_counts(self):
        body = "Ik citeer even:\n\n```\nAKKOORD\n```\n\nmaar ik beslis nog niet."
        _, obs = observe(gate_issue(), [card(), decision(body=body)])
        self.assertIsNone(obs.outcome)

    def test_no_decision_yet_is_not_a_refusal(self):
        _, obs = observe(gate_issue(), [card()])
        self.assertIsNone(obs.outcome)
        self.assertIsNone(obs.refusal)
        self.assertEqual(obs.card_comment_id, "c-card")

    def test_high_risk_refuses_a_bare_akkoord(self):
        issue = gate_issue(labels=("risico/hoog", "poort/wacht-op-mens"))
        _, obs = observe(issue, [card(), decision()])
        self.assertFalse(obs.valid)
        self.assertIn("RISICO-GEZIEN", obs.refusal)

    def test_high_risk_accepts_the_exact_token(self):
        issue = gate_issue(labels=("risico/hoog", "poort/wacht-op-mens"))
        _, obs = observe(issue, [card(), decision(body="AKKOORD RISICO-GEZIEN\n\nGezien.")])
        self.assertTrue(obs.valid)
        self.assertEqual(obs.token, "AKKOORD RISICO-GEZIEN")

    def test_afgekeurd_carries_its_reason_line(self):
        _, obs = observe(gate_issue(), [card(), decision(body="AFGEKEURD\n\nPrijs klopt niet.")])
        self.assertTrue(obs.valid)
        self.assertEqual(obs.outcome, "afgekeurd")


class LabelChannelTests(unittest.TestCase):
    def _history(self, actor=None):
        return {"issue-207": [{
            "createdAt": "2026-09-03T09:30:00.000Z",
            "actor": actor if actor is not None else {"id": APPROVER, "name": "Youp",
                                                      "app": False},
            "addedLabels": [{"id": "l", "name": "akkoord", "parent": {"name": "poort"}}],
            "removedLabels": [],
        }]}

    def test_a_label_flip_with_a_known_actor_is_valid(self):
        issue = gate_issue(labels=("poort/akkoord",))
        client = FakeLinearClient([issue], comments={issue.id: [card()]},
                                  history=self._history())
        obs = gates.evaluate_gate(client, issue, approver_ids=APPROVERS,
                                  dispatcher_user_id=DISPATCHER)
        self.assertTrue(obs.valid)
        self.assertEqual(obs.source, "label")
        self.assertEqual(obs.source_id, "poort/akkoord")
        self.assertEqual(obs.actor_id, APPROVER)

    def test_a_label_flip_by_a_non_approver_is_refused(self):
        issue = gate_issue(labels=("poort/akkoord",))
        client = FakeLinearClient(
            [issue], comments={issue.id: [card()]},
            history=self._history({"id": "user-vreemd", "name": "Iemand", "app": False}))
        obs = gates.evaluate_gate(client, issue, approver_ids=APPROVERS,
                                  dispatcher_user_id=DISPATCHER)
        self.assertFalse(obs.valid)
        self.assertEqual(obs.refusal, gates.REFUSALS[1])

    def test_without_history_the_label_channel_is_refused(self):
        """Zonder actor is voorwaarde 1 tot en met 4 niet te controleren.

        `Issue.history` geeft op deze workspace niets terug, dus een label alleen
        bewijst niet wie het gezet heeft -- en dan gaat de poort niet open.
        """
        issue = gate_issue(labels=("poort/afgekeurd",))
        client = FakeLinearClient([issue], comments={issue.id: [card()]},
                                  history_supported=False)
        obs = gates.evaluate_gate(client, issue, approver_ids=APPROVERS,
                                  dispatcher_user_id=DISPATCHER)
        self.assertFalse(obs.valid)
        self.assertEqual(obs.outcome, "afgekeurd")
        self.assertEqual(obs.refusal, gates.DEGRADED_ACTOR)
        self.assertIsNone(obs.actor_id)

    def test_a_label_flip_older_than_the_card_fails_condition_4(self):
        issue = gate_issue(labels=("poort/akkoord",))
        history = self._history()
        history["issue-207"][0]["createdAt"] = "2026-09-03T08:30:00.000Z"
        client = FakeLinearClient([issue], comments={issue.id: [card()]}, history=history)
        obs = gates.evaluate_gate(client, issue, approver_ids=APPROVERS,
                                  dispatcher_user_id=DISPATCHER)
        self.assertFalse(obs.valid)
        self.assertEqual(obs.refusal, gates.REFUSALS[4])

    def test_a_label_flip_cannot_carry_a_high_risk_acknowledgement(self):
        issue = gate_issue(labels=("poort/akkoord", "risico/hoog"))
        client = FakeLinearClient([issue], comments={issue.id: [card()]},
                                  history_supported=False)
        obs = gates.evaluate_gate(client, issue, approver_ids=APPROVERS,
                                  dispatcher_user_id=DISPATCHER)
        self.assertFalse(obs.valid)
        self.assertIn("RISICO-GEZIEN", obs.refusal)

    def test_the_comment_channel_wins_when_both_are_present(self):
        issue = gate_issue(labels=("poort/akkoord",))
        client = FakeLinearClient([issue], comments={issue.id: [card(), decision()]},
                                  history_supported=False)
        obs = gates.evaluate_gate(client, issue, approver_ids=APPROVERS,
                                  dispatcher_user_id=DISPATCHER)
        self.assertEqual(obs.source, "comment")


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def _apply(self, issue, comment_list, run_id="3f9a2c"):
        client = FakeLinearClient([issue], comments={issue.id: list(comment_list)},
                                  history_supported=False)
        obs = gates.evaluate_gate(client, issue, approver_ids=APPROVERS,
                                  dispatcher_user_id=DISPATCHER)
        target = gates.apply_gate_decision(client, self.store, issue, obs, run_id=run_id)
        return client, obs, target

    def test_approval_moves_the_issue_and_never_sets_a_poort_label(self):
        client, _, target = self._apply(gate_issue(), [card(), decision()])
        self.assertEqual(target, "Na-merge controle")
        updates = [m for m in client.mutations if m.mutation == "issueUpdate"]
        self.assertEqual(updates[-1].variables_summary["stateId"], "<Na-merge controle>")
        added = [label for m in updates for label in m.variables_summary.get("addedLabelIds", [])]
        self.assertNotIn("poort/akkoord", added)
        self.assertIn("poort/vrij", added)
        self.assertIn("poort/wacht-op-mens",
                      updates[-1].variables_summary.get("removedLabelIds", []))
        self.assertIsNone(updates[-1].variables_summary["assigneeId"])

    def test_approval_writes_one_confirmation_comment_naming_the_source(self):
        client, _, _ = self._apply(gate_issue(), [card(), decision()])
        bodies = [c.body for c in client.comments("issue-207") if c.author_id == DISPATCHER]
        self.assertTrue(any("bron comment `c-2`" in body for body in bodies))

    def test_an_invalid_observation_is_refused_by_the_state_machine(self):
        issue = gate_issue()
        client = FakeLinearClient([issue],
                                  comments={issue.id: [card(), decision(author_is_app=True)]})
        obs = gates.evaluate_gate(client, issue, approver_ids=APPROVERS,
                                  dispatcher_user_id=DISPATCHER)
        with self.assertRaises(WriteRefused):
            gates.apply_gate_decision(client, self.store, issue, obs, run_id="3f9a2c")
        self.assertEqual(client.mutations, [])

    def test_a_decision_is_applied_only_once(self):
        issue = gate_issue()
        client = FakeLinearClient([issue], comments={issue.id: [card(), decision()]},
                                  history_supported=False)
        obs = gates.evaluate_gate(client, issue, approver_ids=APPROVERS,
                                  dispatcher_user_id=DISPATCHER)
        self.assertEqual(gates.apply_gate_decision(client, self.store, issue, obs,
                                                   run_id="3f9a2c"), "Na-merge controle")
        writes = len(client.mutations)
        self.assertIsNone(gates.apply_gate_decision(client, self.store, issue, obs,
                                                    run_id="3f9a2c"))
        self.assertEqual(len(client.mutations), writes)

    def test_first_rejection_sends_the_issue_back_with_the_reason_quoted(self):
        client, _, target = self._apply(
            gate_issue(), [card(), decision(body="AFGEKEURD\n\nGeen idempotentie.")])
        self.assertEqual(target, "In uitvoering")
        bodies = [c.body for c in client.comments("issue-207") if c.author_id == DISPATCHER]
        # De woorden van de mens zijn de opdracht voor de volgende ronde, niet
        # het token waarmee hij ze inleidde.
        self.assertTrue(any("> Geen idempotentie." in body for body in bodies), bodies)

    def test_the_reason_on_the_token_line_reaches_the_next_round(self):
        client, _, _ = self._apply(
            gate_issue(), [card(), decision(body="AFGEKEURD: de prijs klopt niet")])
        bodies = [c.body for c in client.comments("issue-207") if c.author_id == DISPATCHER]
        self.assertTrue(any("> de prijs klopt niet" in body for body in bodies), bodies)

    def test_second_rejection_stops_the_issue_without_a_third_attempt(self):
        issue = gate_issue()
        self._apply(issue, [card(), decision(body="AFGEKEURD")])
        client, _, target = self._apply(
            issue, [card(), decision(id="c-3", body="AFGEKEURD",
                                     created_at=T0 + timedelta(hours=2))])
        self.assertIsNone(target)
        updates = [m for m in client.mutations if m.mutation == "issueUpdate"]
        added = [label for m in updates for label in m.variables_summary.get("addedLabelIds", [])]
        self.assertIn("run/vastgelopen", added)
        self.assertFalse(any("stateId" in m.variables_summary for m in updates))
        bodies = [c.body for c in client.comments("issue-207") if c.author_id == DISPATCHER]
        self.assertTrue(any("geen derde poging" in body for body in bodies))

    def test_supervision_time_measures_the_human_and_not_the_poll(self):
        self._apply(gate_issue(), [card(), decision()])
        row = self.store.conn.execute("SELECT * FROM gate_events").fetchone()
        self.assertEqual(row["card_at"], "2026-09-03T09:00:00Z")
        # De beslissing staat 20 minuten na de kaart; de pollronde die hem las
        # mag daar niet bij opgeteld worden.
        self.assertEqual(row["decided_at"], "2026-09-03T09:20:00Z")
        self.assertIsNotNone(row["applied_at"])

    def test_a_label_decision_falls_back_to_the_moment_we_saw_it(self):
        issue = gate_issue(labels=("poort/akkoord", "poort/wacht-op-mens"))
        client = FakeLinearClient([issue], comments={issue.id: [card()]}, history={
            issue.id: [{"createdAt": "2026-09-03T09:30:00.000Z",
                        "actor": {"id": APPROVER, "name": "Youp", "app": False},
                        "addedLabels": [{"id": "l", "name": "akkoord",
                                         "parent": {"name": "poort"}}],
                        "removedLabels": []}]})
        obs = gates.evaluate_gate(client, issue, approver_ids=APPROVERS,
                                  dispatcher_user_id=DISPATCHER)
        gates.apply_gate_decision(client, self.store, issue, obs, run_id="3f9a2c")
        row = self.store.conn.execute("SELECT * FROM gate_events").fetchone()
        self.assertGreater(row["decided_at"], row["card_at"])


class MarkUnconfirmedTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_it_sets_both_labels_and_names_the_failed_condition(self):
        issue = gate_issue()
        client = FakeLinearClient([issue],
                                  comments={issue.id: [card(), decision(author_id="user-x")]})
        obs = gates.evaluate_gate(client, issue, approver_ids=APPROVERS,
                                  dispatcher_user_id=DISPATCHER)
        self.assertTrue(gates.mark_unconfirmed(client, self.store, issue, obs, run_id="3f9a2c"))
        update = [m for m in client.mutations if m.mutation == "issueUpdate"][0]
        self.assertEqual(sorted(update.variables_summary["addedLabelIds"]),
                         ["run/onbevestigd", "schakelaar/mens-vereist"])
        self.assertFalse(any("stateId" in m.variables_summary for m in client.mutations))
        body = client.comments("issue-207")[-1].body
        self.assertIn("voorwaarde 1", body)

    def test_the_busy_label_is_swapped_and_not_stacked(self):
        """`run/` is exclusief: `run/onbevestigd` erbij zonder `run/bezet` eraf faalt.

        Een poortissue mag `run/bezet` dragen -- de cyclus laat dat label daar met
        opzet staan als spoor van een gestrande run -- dus dit is geen randgeval.
        """
        issue = gate_issue()
        self.assertIn("run/bezet", issue.labels)
        client = FakeLinearClient([issue],
                                  comments={issue.id: [card(), decision(author_id="user-x")]})
        obs = gates.evaluate_gate(client, issue, approver_ids=APPROVERS,
                                  dispatcher_user_id=DISPATCHER)
        gates.mark_unconfirmed(client, self.store, issue, obs, run_id="3f9a2c")
        update = [m for m in client.mutations if m.mutation == "issueUpdate"][0]
        self.assertEqual(update.variables_summary["removedLabelIds"], ["run/bezet"])
        self.assertNotIn("run/bezet", client.issue("issue-207").labels)
        self.assertIn("run/onbevestigd", client.issue("issue-207").labels)

    def test_the_same_refusal_is_never_written_twice(self):
        issue = gate_issue()
        client = FakeLinearClient([issue],
                                  comments={issue.id: [card(), decision(author_id="user-x")]})
        obs = gates.evaluate_gate(client, issue, approver_ids=APPROVERS,
                                  dispatcher_user_id=DISPATCHER)
        gates.mark_unconfirmed(client, self.store, issue, obs, run_id="3f9a2c")
        writes = len(client.mutations)
        self.assertFalse(gates.mark_unconfirmed(client, self.store, issue, obs, run_id="4b8d1e"))
        self.assertEqual(len(client.mutations), writes)

    def test_it_refuses_to_run_on_a_valid_observation(self):
        issue = gate_issue()
        client = FakeLinearClient([issue], comments={issue.id: [card(), decision()]},
                                  history_supported=False)
        obs = gates.evaluate_gate(client, issue, approver_ids=APPROVERS,
                                  dispatcher_user_id=DISPATCHER)
        with self.assertRaises(WriteRefused):
            gates.mark_unconfirmed(client, self.store, issue, obs, run_id="3f9a2c")


class EnterGateTests(unittest.TestCase):
    def test_the_six_actions_happen_in_order(self):
        issue = make_issue(state_name="QA op preview", state_type="started",
                           labels=("poort/vrij", "run/bezet"))
        client = FakeLinearClient([issue])
        gates.enter_gate(client, issue, run_id="3f9a2c", gate_state=GATE,
                         approver_id=APPROVER, card_markdown="**Poortkaart merge · WV-207**",
                         artefact_url="https://github.com/raderwerk/x/pull/7")
        kinds = [m.mutation for m in client.mutations]
        self.assertEqual(kinds, ["issueUpdate", "issueUpdate", "issueUpdate", "issueUpdate",
                                 "attachmentLinkURL", "commentCreate"])
        summaries = [m.variables_summary for m in client.mutations]
        self.assertEqual(summaries[0]["stateId"], f"<{GATE}>")
        self.assertEqual(summaries[1]["assigneeId"], APPROVER)
        self.assertIsNone(summaries[1]["delegateId"])
        self.assertEqual(summaries[2]["addedLabelIds"], ["poort/wacht-op-mens"])
        self.assertEqual(sorted(summaries[2]["removedLabelIds"]), ["poort/vrij", "run/bezet"])
        self.assertEqual(summaries[3]["priority"], 1)

    def test_it_skips_the_attachment_when_there_is_no_artefact(self):
        issue = make_issue(state_name="QA op preview", labels=())
        client = FakeLinearClient([issue])
        gates.enter_gate(client, issue, run_id="3f9a2c", gate_state=GATE,
                         approver_id=APPROVER, card_markdown="**Poortkaart**",
                         artefact_url=None)
        self.assertNotIn("attachmentLinkURL", [m.mutation for m in client.mutations])


if __name__ == "__main__":
    unittest.main()
