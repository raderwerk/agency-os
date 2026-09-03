"""Het claimprotocol: één open claim, laagste run-id wint, nooit dubbel schrijven."""

import unittest
from datetime import timedelta

from agency_os.linear import claim as claim_module
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

    def test_the_loser_writes_nothing_after_it_retreats(self):
        client = FakeLinearClient([self.issue], clock=NOW,
                                  comments={"issue-207": [rival_claim("111111")]})
        claim_module.try_claim(client, self.store, self.issue, "999999", settle_s=0, now=NOW)
        after_retreat = [m for m in client.mutations if m.mutation == "commentCreate"]
        self.assertEqual(len(after_retreat), 1)  # alleen de eigen claimcomment

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
