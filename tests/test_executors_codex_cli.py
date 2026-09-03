"""De tweede reviewer: het vaste commando, de diff in de prompt, en het verbruik."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agency_os.executors import codex_cli
from agency_os.executors.codex_cli import CodexCliReviewer, parse_codex_usage
from agency_os.executors.gh import PullRequest
from agency_os.executors.worktree import repo_dir
from tests.executor_fakes import fake_config, git_sandbox, make_issue, make_process, make_request

REVIEW = """Ik heb de diff gelezen.

```json RUNRESULT
{"uitkomst": "klaar", "samenvatting": "Eén blokkerende bevinding.", "dod": "-"}
```
"""

PULL_REQUEST = PullRequest(
    repo="raderwerk/raderwerk-content",
    number=7,
    url="https://github.com/raderwerk/raderwerk-content/pull/7",
    state="OPEN",
    is_draft=False,
    merged=False,
    merged_by_login=None,
    merged_by_is_bot=None,
    head_sha="b" * 40,
    checks_conclusion="success",
)


class CodexUsageTests(unittest.TestCase):
    def test_reads_tokens_when_the_cli_reports_them(self):
        usage = parse_codex_usage("Token usage: total=12,345 input=10,000 (cached 4,000) output=2,345")
        self.assertEqual((usage.tokens_in, usage.tokens_out, usage.cache_read), (10000, 2345, 4000))
        self.assertEqual(usage.source, "codex-cli")
        self.assertTrue(usage.metered)

    def test_without_token_counts_the_run_is_unmetered(self):
        usage = parse_codex_usage("Klaar.\n")
        self.assertFalse(usage.metered)
        self.assertEqual(usage.source, "codex-cli")
        self.assertEqual(usage.cost_usd, 0.0)


class ReviewerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = fake_config(Path(self.tmp.name))
        self.calls: list[dict] = []

    def patch(self, *, pull_request=PULL_REQUEST, stdout=REVIEW, timed_out=False,
              returncode=0, stderr=""):
        def fake_run(cmd, **kwargs):
            self.calls.append({"cmd": list(cmd), **kwargs})
            return make_process(stdout, timed_out=timed_out, returncode=returncode, stderr=stderr)

        patchers = [
            mock.patch.object(codex_cli, "run_process", side_effect=fake_run),
            mock.patch.object(codex_cli, "find_pr_for_branch", return_value=pull_request),
            mock.patch.object(codex_cli, "pr_diff", return_value="diff --git a/x b/x\n"),
            mock.patch.object(codex_cli, "ensure_detached_worktree", side_effect=OSError("geen kloon")),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_review_round_runs_the_frozen_command_with_the_prompt_on_stdin(self):
        self.patch()
        result = CodexCliReviewer(self.cfg).run(make_request(role_key="reviewer-2"))
        self.assertEqual(result.uitkomst, "klaar")
        self.assertEqual(result.summary_md, "Eén blokkerende bevinding.")
        self.assertEqual(
            self.calls[0]["cmd"],
            ["codex", "exec", "-m", "gpt-5.6-sol", "-c", "model_reasoning_effort=xhigh",
             "-c", "notify=[]", "-c", "mcp_servers={}",
             "-c", 'plugins."codex-app-tools@openai-bundled".enabled=false',
             "-s", "read-only", "-"],
        )
        self.assertNotIn("--search", self.calls[0]["cmd"],
                         "codex exec 0.147.0 kent die vlag niet en stopt met afloopcode 2")
        stdin = self.calls[0]["stdin_text"]
        self.assertIn("Doe het werk uit het issue.", stdin)
        self.assertIn("## Diff van PR #7", stdin)
        self.assertIn("diff --git a/x b/x", stdin)

    def test_the_reviewer_gets_no_side_channel_into_linear(self):
        """`codex exec` erft de MCP-servers en app-connectors van wie hem start.

        In de tweede live cyclus zat daar een Linear-connector bij. De reviewer
        probeerde zijn oordeel daarmee zelf als comment weg te schrijven in
        plaats van het op stdout terug te geven; de connector annuleerde die
        schrijfactie en wat overbleef was een run zonder RUNRESULT-blok. De
        enige uitvoerweg van een rol is stdout.
        """
        self.patch()
        CodexCliReviewer(self.cfg).run(make_request())
        cmd = self.calls[0]["cmd"]
        self.assertIn("mcp_servers={}", cmd)
        self.assertIn(f'plugins."{codex_cli.APP_TOOLS_PLUGIN}".enabled=false', cmd)

    def test_the_reviewer_runs_read_only(self):
        """"Wijzig niets" hoort een ontbrekende mogelijkheid te zijn, geen belofte."""
        self.patch()
        CodexCliReviewer(self.cfg).run(make_request())
        cmd = self.calls[0]["cmd"]
        self.assertEqual("read-only", cmd[cmd.index("-s") + 1])

    def test_the_pull_request_url_is_the_evidence_even_without_one_in_the_block(self):
        self.patch()
        result = CodexCliReviewer(self.cfg).run(make_request())
        self.assertEqual(result.pr_url, PULL_REQUEST.url)

    def test_without_a_pull_request_there_is_nothing_to_review(self):
        self.patch(pull_request=None)
        result = CodexCliReviewer(self.cfg).run(make_request())
        self.assertEqual(result.uitkomst, "mislukt")
        self.assertIn("geen PR gevonden", result.error)
        self.assertEqual(self.calls, [])

    def test_a_review_without_a_runresult_block_is_a_failed_run(self):
        self.patch(stdout="Ziet er goed uit wat mij betreft.")
        result = CodexCliReviewer(self.cfg).run(make_request())
        self.assertEqual(result.uitkomst, "mislukt")
        self.assertIn("geen RUNRESULT-blok", result.error)
        self.assertFalse(result.infra_failure, "codex draaide en gaf proza terug")

    def test_an_unknown_flag_is_the_lane_and_costs_the_role_no_turn(self):
        """`error: unexpected argument '--search' found`, afloopcode 2.

        Precies de vorm waarop de reviewer op 2026-09-03 strandde. Daar is geen
        model aan te pas gekomen, dus die poging hoort niet mee te tellen voor
        de lusdetectie.
        """
        self.patch(stdout="", returncode=2, stderr="error: unexpected argument '--search' found")
        result = CodexCliReviewer(self.cfg).run(make_request())
        self.assertEqual(result.uitkomst, "mislukt")
        self.assertTrue(result.infra_failure)
        self.assertIn("codex foutcode 2", result.error)

    def test_a_finished_review_is_never_an_infrastructure_failure(self):
        self.patch()
        self.assertFalse(CodexCliReviewer(self.cfg).run(make_request()).infra_failure)

    def test_a_round_without_a_pull_request_is_an_infrastructure_failure(self):
        self.patch(pull_request=None)
        self.assertTrue(CodexCliReviewer(self.cfg).run(make_request()).infra_failure)

    def test_a_timeout_becomes_afgebroken(self):
        self.patch(timed_out=True)
        result = CodexCliReviewer(self.cfg).run(make_request())
        self.assertEqual(result.uitkomst, "afgebroken")
        self.assertEqual(result.usage.source, "codex-cli")

    def test_a_dry_run_never_starts_codex(self):
        self.patch()
        result = CodexCliReviewer(self.cfg).run(make_request(dry_run=True))
        self.assertEqual(result.uitkomst, "afgebroken")
        self.assertEqual(self.calls, [])

    def test_a_round_without_a_repository_is_refused(self):
        self.patch()
        result = CodexCliReviewer(self.cfg).run(make_request(repo=None))
        self.assertEqual(result.uitkomst, "mislukt")


class DetachedCheckoutTests(unittest.TestCase):
    """De reviewer draait op een losse uitcheck van de PR-kop en ruimt hem op."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = git_sandbox(self.root, "raderwerk/agency-os")
        self.head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir(self.cfg, "raderwerk/agency-os"),
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.calls: list[dict] = []

    def test_the_checkout_is_used_and_removed_again(self):
        def fake_run(cmd, **kwargs):
            self.calls.append({"cmd": list(cmd), **kwargs})
            return make_process(REVIEW)

        pull_request = replace(PULL_REQUEST, repo="raderwerk/agency-os", head_sha=self.head)
        with mock.patch.object(codex_cli, "run_process", side_effect=fake_run), \
             mock.patch.object(codex_cli, "find_pr_for_branch", return_value=pull_request), \
             mock.patch.object(codex_cli, "pr_diff", return_value="diff\n"):
            result = CodexCliReviewer(self.cfg).run(
                make_request(repo="raderwerk/agency-os", issue=make_issue(identifier="WV-207"))
            )

        self.assertEqual(result.uitkomst, "klaar")
        used = Path(self.calls[0]["cwd"])
        self.assertEqual(used, self.cfg.worktree_root / "agency-os" / "WV-207-review")
        self.assertFalse(used.exists(), "de reviewmap moet na afloop opgeruimd zijn")


if __name__ == "__main__":
    unittest.main()
