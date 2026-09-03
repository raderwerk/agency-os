"""Het claimprotocol: één open claim, laagste run-id wint, nooit dubbel schrijven."""

import unittest
from datetime import timedelta

from agency_os.linear import claim as claim_module
from agency_os.linear.client import LinearError
from agency_os.linear.store import Store

from tests.support_linear import (
    DISPATCHER,
    T0,
    FakeLinearClient,
    make_comment,
    make_issue,
)

NOW = lambda: T0  # noqa: E731 - een vaste klok maakt het settle-venster toetsbaar


def rival_claim(run_id, minutes=0):
    return make_comment(
        id=f"c-{run_id}", body=f"**Spil** claim {run_id} op 2026-09-03 11:00",
        created_at=T0 + timedelta(minutes=minutes), author_id=DISPATCHER,
        author_name="Spil", author_is_app=True)


class TryClaimTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.issue = make_issue(labels=("soort/contentstuk", "run/wachtrij"))

    def test_a_clean_claim_sets_the_label_and_writes_one_comment(self):
        client = FakeLinearClient([self.issue])
        result = claim_module.try_claim(client, self.store, self.issue, "3f9a2c", settle_s=0)
        self.assertIsNotNone(result)
        self.assertEqual(result.issue_identifier, "WV-207")
        update = [m for m in client.mutations if m.mutation == "issueUpdate"][0]
        self.assertEqual(update.variables_summary["addedLabelIds"], ["run/bezet"])
        self.assertEqual(update.variables_summary["removedLabelIds"], ["run/wachtrij"])
        self.assertEqual(
            len([m for m in client.mutations if m.mutation == "commentCreate"]), 1)

    def test_the_claim_comment_carries_the_run_id(self):
        client = FakeLinearClient([self.issue])
        claim_module.try_claim(client, self.store, self.issue, "3f9a2c", settle_s=0)
        self.assertIn("**Spil** claim 3f9a2c op", client.comments("issue-207")[0].body)

    def test_a_finished_issue_can_be_claimed_by_the_next_role(self):
        """De blokkade uit de tweede live cyclus: WV-210 op Agentreview.

        Na een geslaagde run draagt het issue `run/klaar`. De groep `run/` is
        exclusief, dus `run/bezet` erbij zetten zonder `run/klaar` weg te halen
        laat Linear de hele issueUpdate weigeren met `labelIds not exclusive
        child labels` -- en dan komt geen enkel issue ooit voorbij zijn eerste
        rol.
        """
        done = make_issue(labels=("soort/contentstuk", "run/klaar"))
        client = FakeLinearClient([done])
        result = claim_module.try_claim(client, self.store, done, "3f9a2c", settle_s=0)
        self.assertIsNotNone(result)
        update = [m for m in client.mutations if m.mutation == "issueUpdate"][0]
        self.assertEqual(update.variables_summary["addedLabelIds"], ["run/bezet"])
        self.assertEqual(update.variables_summary["removedLabelIds"], ["run/klaar"])
        self.assertEqual(client.issue("issue-207").labels.count("run/bezet"), 1)
        self.assertNotIn("run/klaar", client.issue("issue-207").labels)

    def test_a_failed_run_label_is_swapped_too(self):
        failed = make_issue(labels=("soort/contentstuk", "run/mislukt"))
        client = FakeLinearClient([failed])
        claim_module.try_claim(client, self.store, failed, "3f9a2c", settle_s=0)
        update = [m for m in client.mutations if m.mutation == "issueUpdate"][0]
        self.assertEqual(update.variables_summary["removedLabelIds"], ["run/mislukt"])

    def test_a_refused_label_write_does_not_leave_an_open_claim_behind(self):
        """Een mislukte claim mag het issue geen half uur op slot zetten.

        `insert_claim` gaat vooraf aan de labelwissel. Gooit die wissel, dan is
        de rij in sqlite van niemand: `try_claim` geeft er daarna eeuwig None op
        terug tot de verzoening bij het opstarten hem na SPIL_RUN_TIMEOUT_S
        vrijgeeft.
        """
        client = FakeLinearClient([self.issue])
        client.fail_next("issueUpdate", LinearError("labelIds not exclusive child labels"))
        with self.assertRaises(LinearError):
            claim_module.try_claim(client, self.store, self.issue, "3f9a2c", settle_s=0)
        self.assertEqual(self.store.open_claims(), [])

        self.assertIsNotNone(
            claim_module.try_claim(client, self.store, self.issue, "4b8d1e", settle_s=0),
            "de volgende ronde moet het gewoon opnieuw kunnen proberen")

    def test_a_second_run_id_cannot_claim_the_same_issue(self):
        client = FakeLinearClient([self.issue])
        first = claim_module.try_claim(client, self.store, self.issue, "111111", settle_s=0)
        second = claim_module.try_claim(client, self.store, self.issue, "222222", settle_s=0)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(self.store.open_claims()), 1)

    def test_the_lowest_run_id_wins_the_settle_window(self):
        client = FakeLinearClient([self.issue], clock=NOW,
                                  comments={"issue-207": [rival_claim("111111")]})
        result = claim_module.try_claim(client, self.store, self.issue, "999999", settle_s=0,
                                        now=NOW)
        self.assertIsNone(result)
        self.assertIsNone(self.store.open_claim("issue-207"))

    def test_the_loser_writes_no_second_comment_and_hands_the_label_back(self):
        client = FakeLinearClient([self.issue], clock=NOW,
                                  comments={"issue-207": [rival_claim("111111")]})
        claim_module.try_claim(client, self.store, self.issue, "999999", settle_s=0, now=NOW)

        after_retreat = [m for m in client.mutations if m.mutation == "commentCreate"]
        self.assertEqual(len(after_retreat), 1)  # alleen de eigen claimcomment
        # Het label mag niet blijven hangen: `run/bezet` zonder open claim maakt
        # het issue voorgoed onclaimbaar.
        self.assertNotIn("run/bezet", client.issue("issue-207").labels)
        self.assertIn("run/wachtrij", client.issue("issue-207").labels)
        self.assertEqual(self.store.open_claims(), [])

    def test_a_loser_that_never_set_the_label_leaves_it_alone(self):
        busy = make_issue(labels=("run/bezet",))
        client = FakeLinearClient([busy], clock=NOW,
                                  comments={"issue-207": [rival_claim("111111")]})
        claim_module.try_claim(client, self.store, busy, "999999", settle_s=0, now=NOW)
        self.assertEqual([m for m in client.mutations if m.mutation == "issueUpdate"], [])

    def test_two_runs_back_to_back_do_not_fight_over_a_closed_claim(self):
        """`run --once` twee keer achter elkaar, zoals het leesmij voorschrijft."""
        client = FakeLinearClient([self.issue], clock=NOW)
        first = claim_module.try_claim(client, self.store, self.issue, "aaaaaa", settle_s=0,
                                       now=NOW)
        claim_module.release_claim(client, self.store, first, final_label="run/klaar")

        second = claim_module.try_claim(client, self.store, client.issue("issue-207"), "bbbbbb",
                                        settle_s=0, now=NOW)
        self.assertIsNotNone(second, "de vorige run is klaar en is geen tegenstander meer")
        self.assertIn("run/bezet", client.issue("issue-207").labels)

    def test_a_rival_that_predates_the_settle_window_never_counts(self):
        """Een claimcomment van een minuut oud valt buiten een venster van 5 seconden."""
        client = FakeLinearClient([self.issue], clock=NOW,
                                  comments={"issue-207": [rival_claim("111111", minutes=-1)]})
        self.assertFalse(claim_module._lost_the_settle_window(
            client, self.store, "issue-207", "999999", T0, 5.0))
        self.assertTrue(claim_module._lost_the_settle_window(
            client, self.store, "issue-207", "999999", T0 - timedelta(minutes=1), 5.0))

    def test_a_higher_rival_run_id_does_not_win(self):
        client = FakeLinearClient([self.issue], clock=NOW,
                                  comments={"issue-207": [rival_claim("ffffff")]})
        self.assertIsNotNone(
            claim_module.try_claim(client, self.store, self.issue, "111111", settle_s=0,
                                   now=NOW))

    def test_an_old_claim_comment_is_outside_the_settle_window(self):
        client = FakeLinearClient([self.issue], clock=NOW,
                                  comments={"issue-207": [rival_claim("111111", minutes=-60)]})
        self.assertIsNotNone(
            claim_module.try_claim(client, self.store, self.issue, "999999", settle_s=0,
                                   now=NOW))

    def test_a_restart_writes_no_second_comment(self):
        client = FakeLinearClient([self.issue])
        claim_module.try_claim(client, self.store, self.issue, "3f9a2c", settle_s=0)
        before = len([m for m in client.mutations if m.mutation == "commentCreate"])
        claim_module.try_claim(client, self.store, self.issue, "3f9a2c", settle_s=0)
        after = len([m for m in client.mutations if m.mutation == "commentCreate"])
        self.assertEqual(before, after)

    def test_already_ran_reports_a_restart(self):
        client = FakeLinearClient([self.issue])
        self.assertFalse(claim_module.already_ran(self.store, "issue-207", "3f9a2c"))
        claim_module.try_claim(client, self.store, self.issue, "3f9a2c", settle_s=0)
        self.assertTrue(claim_module.already_ran(self.store, "issue-207", "3f9a2c"))

    def test_existing_run_comment_finds_a_comment_by_run_id(self):
        client = FakeLinearClient([self.issue], comments={"issue-207": [
            make_comment(id="c-9", body="**Redacteur · Claude Sonnet 5 · run abc123 · nu**")]})
        self.assertEqual(
            claim_module.existing_run_comment(client, "issue-207", "abc123"), "c-9")
        self.assertIsNone(claim_module.existing_run_comment(client, "issue-207", "zzzzzz"))

    def test_an_issue_that_is_already_busy_is_not_relabelled(self):
        busy = make_issue(labels=("run/bezet",))
        client = FakeLinearClient([busy])
        claim_module.try_claim(client, self.store, busy, "3f9a2c", settle_s=0)
        self.assertEqual([m for m in client.mutations if m.mutation == "issueUpdate"], [])


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.issue = make_issue(labels=("run/bezet",))
        self.client = FakeLinearClient([self.issue])

    def test_release_swaps_the_run_label_and_closes_the_row(self):
        claim = claim_module.try_claim(self.client, self.store, self.issue, "3f9a2c",
                                       settle_s=0)
        claim_module.release_claim(self.client, self.store, claim, final_label="run/klaar")
        self.assertIsNone(self.store.open_claim("issue-207"))
        last = [m for m in self.client.mutations if m.mutation == "issueUpdate"][-1]
        self.assertEqual(last.variables_summary["addedLabelIds"], ["run/klaar"])
        self.assertEqual(last.variables_summary["removedLabelIds"], ["run/bezet"])

    def test_an_unknown_final_label_is_refused(self):
        claim = claim_module.try_claim(self.client, self.store, self.issue, "3f9a2c",
                                       settle_s=0)
        with self.assertRaises(ValueError):
            claim_module.release_claim(self.client, self.store, claim, final_label="run/mooi")

    def test_after_release_the_issue_can_be_claimed_again(self):
        self.client.clock = NOW
        claim = claim_module.try_claim(self.client, self.store, self.issue, "111111",
                                       settle_s=0, now=NOW)
        claim_module.release_claim(self.client, self.store, claim, final_label="run/wachtrij")
        later = T0 + timedelta(hours=1)
        self.client.clock = lambda: later
        self.assertIsNotNone(
            claim_module.try_claim(self.client, self.store, self.issue, "222222", settle_s=0,
                                   now=lambda: later))


class StoreLockTests(unittest.TestCase):
    def test_sqlite_enforces_one_open_claim_per_issue(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        self.assertTrue(store.insert_claim("issue-1", "aaaaaa", "WV-1", T0))
        self.assertFalse(store.insert_claim("issue-1", "bbbbbb", "WV-1", T0))
        store.release_claim("issue-1", "aaaaaa", "klaar")
        self.assertTrue(store.insert_claim("issue-1", "bbbbbb", "WV-1", T0))


if __name__ == "__main__":
    unittest.main()
