"""De native lanen: de trigger-comment, de statusafbeelding en de terugval."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from agency_os import gate
from agency_os.executors.base import ExecutorError, TriggerReceipt
from agency_os.executors.native import (
    FALLBACK_ERROR,
    NativeExecutor,
    extract_pr_url,
    fallback_comment,
    mention_body,
)
from tests.executor_fakes import (
    NOW,
    FakeActivity,
    FakeClient,
    fake_config,
    make_issue,
    make_request,
    make_session,
)

PR_URL = "https://github.com/raderwerk/raderwerk-content/pull/7"


class MentionBodyTests(unittest.TestCase):
    def test_the_codex_form_names_the_repository_on_the_first_line(self):
        body = mention_body("codex", make_request())
        first = body.splitlines()[0]
        self.assertTrue(first.startswith("@Codex "))
        self.assertTrue(first.endswith(" in raderwerk/raderwerk-content"))

    def test_the_cursor_form_carries_repo_and_branch_on_the_first_line(self):
        body = mention_body("cursor", make_request())
        first = body.splitlines()[0]
        self.assertTrue(first.startswith("@Cursor "))
        self.assertIn("repo=raderwerk/raderwerk-content", first)
        self.assertIn("branch=main", first)

    def test_the_body_carries_issue_branch_and_the_no_merge_rule(self):
        body = mention_body("codex", make_request())
        self.assertIn("WV-207", body)
        self.assertIn("feat/WV-207-publiek-bouwlogboek", body)
        self.assertIn("merge niet", body)

    def test_a_native_lane_without_a_repository_is_refused(self):
        with self.assertRaises(ExecutorError):
            mention_body("codex", make_request(repo=None))

    def test_the_body_can_never_open_a_gate(self):
        for agent in ("codex", "cursor"):
            gate.assert_not_gate_opening(mention_body(agent, make_request()), author_is_agent=True)


class ExtractPrUrlTests(unittest.TestCase):
    def test_from_the_pull_requests_field(self):
        self.assertEqual(extract_pr_url(make_session(pull_request_url=PR_URL)), PR_URL)

    def test_from_the_summary(self):
        session = make_session(summary=f"Klaar. PR staat op {PR_URL} .")
        self.assertEqual(extract_pr_url(session), PR_URL)

    def test_from_an_activity(self):
        session = make_session(
            summary="Klaar.",
            activities=(FakeActivity(body=f"Ik heb {PR_URL} geopend."),),
        )
        self.assertEqual(extract_pr_url(session), PR_URL)

    def test_no_link_at_all(self):
        self.assertIsNone(extract_pr_url(make_session(summary="Ik kom er niet uit.")))

    def test_a_link_to_another_organisation_is_not_ours(self):
        session = make_session(summary="Zie https://github.com/fightclub/portal/pull/3")
        self.assertIsNone(extract_pr_url(session))


class NativeExecutorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = fake_config(Path(self.tmp.name))
        self.client = FakeClient()
        self.executor = NativeExecutor(self.cfg, "codex")
        self.issue = make_issue()

    def receipt(self, **overrides) -> TriggerReceipt:
        base = TriggerReceipt(
            run_id="3f9a2c",
            issue_id=self.issue.id,
            executor="native-codex",
            trigger_comment_id="comment-1",
            session_id=None,
            triggered_at=NOW,
            strikes=0,
        )
        return replace(base, **overrides)

    def seed(self, *sessions):
        self.client.sessions[self.issue.id] = list(sessions)


class TriggerTests(NativeExecutorTestCase):
    def test_the_comment_is_the_trigger(self):
        receipt = self.executor.trigger(self.client, make_request())
        self.assertEqual(len(self.client.comments), 1)
        self.assertTrue(self.client.comments[0]["body"].startswith("@Codex "))
        self.assertEqual(receipt.executor, "native-codex")
        self.assertEqual(receipt.trigger_comment_id, "comment-1")
        self.assertEqual(receipt.strikes, 0)

    def test_a_dry_run_writes_nothing(self):
        receipt = self.executor.trigger(self.client, make_request(dry_run=True))
        self.assertEqual(self.client.comments, [])
        self.assertIsNone(receipt.trigger_comment_id)

    def test_an_unknown_agent_is_refused(self):
        with self.assertRaises(ValueError):
            NativeExecutor(self.cfg, "chatgpt")


class PollStatusTests(NativeExecutorTestCase):
    def test_a_running_session_keeps_waiting_and_resets_the_strikes(self):
        self.seed(make_session(status="active"))
        receipt, result = self.executor.poll(self.client, self.receipt(strikes=1), self.issue)
        self.assertIsNone(result)
        self.assertEqual(receipt.strikes, 0)
        self.assertEqual(receipt.session_id, "sessie-1")

    def test_a_running_session_claims_the_delegate_once(self):
        self.seed(make_session(status="pending"))
        self.executor.poll(self.client, self.receipt(), self.issue)
        self.assertEqual(self.client.updates[0]["delegate_id"], "app-codex")

        self.client.updates.clear()
        self.executor.poll(self.client, self.receipt(), replace(self.issue, delegate_id="app-codex"))
        self.assertEqual(self.client.updates, [])

    def test_a_completed_session_is_a_finished_unmetered_run(self):
        self.seed(make_session(status="complete", summary="Klaar.", pull_request_url=PR_URL))
        _, result = self.executor.poll(self.client, self.receipt(), self.issue)
        self.assertEqual(result.uitkomst, "klaar")
        self.assertEqual(result.pr_url, PR_URL)
        self.assertEqual([a.type for a in result.artifacts], ["pr"])
        self.assertFalse(result.usage.metered)
        self.assertEqual(result.usage.source, "native-unmetered")

    def test_a_completed_session_without_a_pull_request_says_so(self):
        self.seed(make_session(status="complete", summary="Klaar."))
        _, result = self.executor.poll(self.client, self.receipt(), self.issue)
        self.assertEqual(result.uitkomst, "klaar")
        self.assertIsNone(result.pr_url)
        self.assertIn("geen PR-link", result.summary_md)

    def test_a_session_of_another_app_is_not_ours(self):
        self.seed(make_session(app_user_name="Cursor", status="complete"))
        receipt, result = self.executor.poll(self.client, self.receipt(), self.issue)
        self.assertIsNone(result)
        self.assertIsNone(receipt.session_id)

    def test_a_session_from_before_the_trigger_is_not_ours(self):
        self.seed(make_session(created_at=NOW - timedelta(hours=2), status="complete"))
        _, result = self.executor.poll(self.client, self.receipt(), self.issue)
        self.assertIsNone(result)


class FallbackTests(NativeExecutorTestCase):
    def test_the_first_strike_only_counts(self):
        self.seed(make_session(status="awaitingInput"))
        receipt, result = self.executor.poll(self.client, self.receipt(), self.issue)
        self.assertIsNone(result)
        self.assertEqual(receipt.strikes, 1)
        self.assertEqual(self.client.comments, [])

    def test_the_second_strike_hands_the_issue_back(self):
        self.seed(make_session(status="awaitingInput"))
        receipt, result = self.executor.poll(self.client, self.receipt(strikes=1), self.issue)

        self.assertEqual(receipt.strikes, 2)
        self.assertEqual(result.uitkomst, "mislukt")
        self.assertTrue(result.error.startswith(FALLBACK_ERROR))
        self.assertFalse(result.usage.metered)

        update = self.client.updates[0]
        self.assertTrue(update["clear_delegate"])
        self.assertIn("run/vastgelopen", update["added_labels"])

        body = self.client.comments[0]["body"]
        self.assertIn("De tweede reviewer (Codex) was niet beschikbaar", body)
        self.assertIn("awaitingInput", body)
        self.assertIn("geen volwaardige dubbele review", body)

    def test_every_stuck_status_counts_towards_the_fallback(self):
        for status in ("awaitingInput", "error", "stale"):
            with self.subTest(status=status):
                self.setUp()
                self.seed(make_session(status=status))
                _, result = self.executor.poll(self.client, self.receipt(strikes=1), self.issue)
                self.assertEqual(result.uitkomst, "mislukt")

    def test_a_lost_second_review_on_a_high_risk_issue_marks_the_missing_evidence(self):
        issue = make_issue(state_name="Agentreview", risico="hoog")
        self.client.sessions[issue.id] = [make_session(status="error")]
        self.executor.poll(self.client, self.receipt(strikes=1), issue)
        self.assertIn("bewijs-ontbreekt", self.client.updates[0]["added_labels"])

    def test_a_low_risk_issue_does_not_get_that_label(self):
        self.seed(make_session(status="error"))
        self.executor.poll(self.client, self.receipt(strikes=1), self.issue)
        self.assertNotIn("bewijs-ontbreekt", self.client.updates[0]["added_labels"])

    def test_a_session_that_never_appears_falls_back_after_the_deadline(self):
        self.seed()
        receipt = self.receipt(triggered_at=NOW - timedelta(days=1))
        _, result = self.executor.poll(self.client, receipt, self.issue)
        self.assertEqual(result.uitkomst, "mislukt")
        self.assertIn("geen sessie", self.client.comments[0]["body"])

    def test_a_session_that_never_finishes_falls_back_after_the_deadline(self):
        self.seed(make_session(status="active", created_at=NOW - timedelta(hours=23)))
        receipt = self.receipt(triggered_at=NOW - timedelta(hours=23, minutes=59))
        _, result = self.executor.poll(self.client, receipt, self.issue)
        self.assertEqual(result.uitkomst, "mislukt")

    def test_the_fallback_comment_is_the_literal_roster_sentence(self):
        """De enige plek waar deze tekst nog staat: native.py schrijft hem echt."""
        body = fallback_comment("Codex", "awaitingInput", NOW, "3f9a2c")
        self.assertIn("De tweede reviewer (Codex) was niet beschikbaar", body)
        self.assertIn("sessie stond op awaitingInput", body)
        self.assertIn("Dit is geen volwaardige dubbele review.", body)

    def test_the_fallback_comment_can_never_open_a_gate(self):
        self.seed(make_session(status="stale"))
        self.executor.poll(self.client, self.receipt(strikes=1), self.issue)
        gate.assert_not_gate_opening(self.client.comments[0]["body"], author_is_agent=True)


if __name__ == "__main__":
    unittest.main()
