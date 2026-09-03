"""Het bewijsblok: wat QA en de Reviewer over de artefacten te zien krijgen.

Zonder dit blok beoordeelt QA een pull request die hij niet kan aanwijzen, met
een CI-uitslag die hij niet kan lezen en een preview die hij niet kent -- en
`gh` heeft hij niet, die staat op de weigerlijst van de Claude-laan.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests.executor_fakes import fake_config
from tests.fakes import make_issue

from agency_os.app import evidence
from agency_os.app.prompts import build_prompt
from agency_os.app.routing import load_table
from agency_os.executors.gh import ChecksSummary, PagesSite, PullRequest
from agency_os.linear.models import CommentView

TABLE = load_table()
BRANCH = "feat/WV-210-prijskaart-en-dienstenmatrix"
REPO = "raderwerk/raderwerk-content"

PULL_REQUEST = PullRequest(
    repo=REPO,
    number=2,
    url=f"https://github.com/{REPO}/pull/2",
    state="OPEN",
    is_draft=False,
    merged=False,
    merged_by_login=None,
    merged_by_is_bot=None,
    head_sha="2db955a1c0ffee00deadbeef1234567890abcdef",
    checks_conclusion="success",
    head_branch=BRANCH,
)
GREEN = ChecksSummary(verdict="geslaagd", total=3, passed=3)


def a_comment(body: str, *, minute: int = 0, who: str = "Spil") -> CommentView:
    return CommentView(id=f"c-{minute}", body=body,
                       created_at=datetime(2026, 9, 3, 14, minute, tzinfo=timezone.utc),
                       author_id="user-spil", author_name=who, author_is_app=True)


class EvidenceBlockTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = fake_config(Path(tmp.name))
        self.issue = make_issue(identifier="WV-210")

    def patch(self, *, pull_request=PULL_REQUEST, checks=GREEN, pages=None):
        for name, value in (("find_pr_for_branch", pull_request),
                            ("pr_checks", checks),
                            ("pages_site", pages)):
            patcher = mock.patch.object(evidence, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)


class WithAPullRequestTests(EvidenceBlockTestCase):
    def test_the_block_carries_the_pull_request_ci_branch_and_head(self):
        self.patch(pages=PagesSite(url="https://raderwerk.github.io/raderwerk-content/",
                                   branch="main"))
        block = evidence.evidence_block(self.cfg, self.issue, branch=BRANCH)

        self.assertIn("PR #2", block)
        self.assertIn(f"https://github.com/{REPO}/pull/2", block)
        self.assertIn("open", block)
        self.assertIn("CI: geslaagd, 3 van 3 checks groen", block)
        self.assertIn(f"`{BRANCH}`", block)
        self.assertIn("HEAD 2db955a1c0ff", block)

    def test_a_preview_on_another_branch_is_marked_as_only_after_the_merge(self):
        self.patch(pages=PagesSite(url="https://raderwerk.github.io/raderwerk-content/",
                                   branch="main"))
        block = evidence.evidence_block(self.cfg, self.issue, branch=BRANCH)
        self.assertIn("https://raderwerk.github.io/raderwerk-content/", block)
        self.assertIn("pas ná merge", block)

    def test_a_preview_that_is_this_branch_says_so(self):
        self.patch(pages=PagesSite(url="https://raderwerk.github.io/raderwerk-content/",
                                   branch=BRANCH))
        block = evidence.evidence_block(self.cfg, self.issue, branch=BRANCH)
        self.assertIn("deze branch staat erop", block)
        self.assertNotIn("pas ná merge", block)

    def test_a_repository_without_pages_has_no_preview_to_promise(self):
        self.patch()
        block = evidence.evidence_block(self.cfg, self.issue, branch=BRANCH)
        self.assertIn("publiceert geen GitHub Pages", block)
        self.assertNotIn("raderwerk.github.io", block)

    def test_an_unreadable_ci_status_is_said_out_loud(self):
        self.patch(checks=ChecksSummary("niet op te halen"))
        block = evidence.evidence_block(self.cfg, self.issue, branch=BRANCH)
        self.assertIn("CI: niet op te halen", block)
        self.assertIn("niet te verifiëren", block)

    def test_a_red_check_is_named(self):
        self.patch(checks=ChecksSummary(verdict="mislukt", total=3, passed=2, failed=1,
                                        failing=("tests",)))
        block = evidence.evidence_block(self.cfg, self.issue, branch=BRANCH)
        self.assertIn("CI: mislukt, 2 van 3 checks groen; rood: tests", block)

    def test_a_broken_gh_costs_the_run_nothing(self):
        """Een `gh` die niet kan inloggen mag geen uitzondering de prompt in duwen."""
        with mock.patch.object(evidence, "find_pr_for_branch", side_effect=OSError("geen gh")):
            block = evidence.evidence_block(self.cfg, self.issue, branch=BRANCH)
        self.assertIn("Er is geen pull request", block)


class WithoutAPullRequestTests(EvidenceBlockTestCase):
    def test_the_block_explains_the_absence_instead_of_staying_silent(self):
        self.patch(pull_request=None)
        block = evidence.evidence_block(self.cfg, self.issue, branch=BRANCH)

        self.assertIn("Er is geen pull request", block)
        self.assertIn(BRANCH, block)
        self.assertIn("niet te verifiëren", block)
        self.assertIn("geen HEAD-sha", block)
        self.assertNotIn("https://github.com/", block, "geen verzonnen PR-link")
        self.assertNotIn("- CI:", block, "geen lege CI-regel die groen kan lijken")

    def test_an_issue_without_a_repository_says_that_too(self):
        self.patch(pull_request=None)
        issue = make_issue(labels=("soort/contentstuk",), contract=None)
        block = evidence.evidence_block(self.cfg, issue, branch="")
        self.assertIn("noemt geen repo", block)


class VerdictPointerTests(EvidenceBlockTestCase):
    """De oordelen staan al voluit in het discussieblok; hier staat de wegwijzer."""

    def setUp(self):
        super().setUp()
        self.patch(pull_request=None)
        self.thread = [
            a_comment("**Ontwikkelaar · Claude Opus 5 · run 0efc45 · 2026-09-03 13:36**\n\nGebouwd.",
                      minute=1),
            a_comment("**Reviewer 1 · Codex GPT-5.6 Sol · run b719b0 · 2026-09-03 14:13**\n\n"
                      "Blokkerende bevinding over het uurtarief.", minute=13),
            a_comment("**Reviewer 1 · Codex GPT-5.6 Sol · run c8d2e1 · 2026-09-03 14:40**\n\n"
                      "Hersteld, akkoord.", minute=40),
        ]

    def test_only_the_reviewing_roles_are_pointed_at_and_only_once(self):
        block = evidence.evidence_block(self.cfg, self.issue, branch=BRANCH,
                                        discussion=self.thread)
        self.assertEqual(1, block.count("Reviewer 1:"), "alleen het nieuwste oordeel")
        self.assertIn("2026-09-03 14:40 UTC", block)
        self.assertNotIn("Ontwikkelaar", block)

    def test_the_text_of_the_verdict_is_not_repeated(self):
        block = evidence.evidence_block(self.cfg, self.issue, branch=BRANCH,
                                        discussion=self.thread)
        self.assertNotIn("Hersteld, akkoord.", block)
        self.assertIn("Discussie op het issue", block)

    def test_an_empty_thread_leaves_no_dangling_heading(self):
        block = evidence.evidence_block(self.cfg, self.issue, branch=BRANCH)
        self.assertNotIn("Eerdere oordelen", block)


class ForRoleTests(EvidenceBlockTestCase):
    def test_the_reviewing_roles_get_the_block(self):
        self.patch()
        for key in ("reviewer", "qa", "qa-rookproef"):
            with self.subTest(key):
                extra = evidence.for_role(self.cfg, TABLE.roles[key], self.issue, branch=BRANCH)
                self.assertIn(evidence.EVIDENCE_HEADING, extra)

    def test_a_making_role_gets_nothing_and_gh_is_never_called(self):
        with mock.patch.object(evidence, "find_pr_for_branch") as finder:
            extra = evidence.for_role(self.cfg, TABLE.roles["redacteur"], self.issue, branch=BRANCH)
        self.assertEqual({}, dict(extra))
        finder.assert_not_called()


class PromptIntegrationTests(EvidenceBlockTestCase):
    """De haak bestond al; hij werd alleen door niemand gevuld."""

    class Cfg:
        class executors:
            repo_root = "/bestaat/niet"

    def test_the_block_ends_up_in_the_prompt_of_the_qa_role(self):
        self.patch(pages=PagesSite(url="https://raderwerk.github.io/raderwerk-content/",
                                   branch="main"))
        extra = evidence.for_role(self.cfg, TABLE.roles["qa"], self.issue, branch=BRANCH)
        prompt = build_prompt(self.Cfg(), TABLE.roles["qa"], self.issue, run_id="a1b2c3",
                              branch=BRANCH, extra_context=extra)

        self.assertIn(f"## {evidence.EVIDENCE_HEADING}", prompt)
        self.assertIn(f"https://github.com/{REPO}/pull/2", prompt)
        self.assertLess(prompt.index("Onwrikbare regels"),
                        prompt.index(evidence.EVIDENCE_HEADING))

    def test_a_making_role_keeps_the_prompt_it_had(self):
        prompt = build_prompt(self.Cfg(), TABLE.roles["redacteur"], self.issue, run_id="a1b2c3",
                              branch=BRANCH, extra_context={})
        self.assertNotIn(evidence.EVIDENCE_HEADING, prompt)


if __name__ == "__main__":
    unittest.main()
