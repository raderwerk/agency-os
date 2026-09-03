"""De Claude-rolrunner: JSON lezen, RUNRESULT, tijdslimiet en de gevaarlijke vlag."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agency_os.executors import claude_runner
from agency_os.executors.claude_runner import (
    SKIP_PERMISSIONS,
    ClaudeRunner,
    parse_claude_json,
)
from tests.executor_fakes import fake_config, git_sandbox, make_issue, make_process, make_request

RUNRESULT = """Ik heb het bouwlogboek geschreven.

```json RUNRESULT
{"uitkomst": "klaar",
 "samenvatting": "Vier weken bouwlogboek toegevoegd.",
 "dod": "6/6",
 "vraag": null,
 "pr_url": null,
 "bewijs": [{"type": "test", "url": "https://example.invalid/ci", "label": "CI groen"}]}
```
"""


def claude_stdout(result: str = RUNRESULT, **overrides) -> str:
    payload = {
        "type": "result",
        "result": result,
        "session_id": "sess-1",
        "num_turns": 12,
        "duration_ms": 90_000,
        "total_cost_usd": 0.42,
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 250,
            "cache_read_input_tokens": 4000,
        },
    }
    payload.update(overrides)
    return json.dumps(payload)


class ParseClaudeJsonTests(unittest.TestCase):
    def test_reads_every_field(self):
        text, usage, session_id = parse_claude_json(claude_stdout())
        self.assertIn("RUNRESULT", text)
        self.assertEqual(session_id, "sess-1")
        self.assertEqual((usage.tokens_in, usage.tokens_out, usage.cache_read), (1000, 250, 4000))
        self.assertEqual(usage.turns, 12)
        self.assertEqual(usage.cost_usd, 0.42)
        self.assertEqual(usage.duration_s, 90.0)
        self.assertEqual(usage.source, "claude-json")
        self.assertTrue(usage.metered)

    def test_missing_fields_become_zero_and_unknown(self):
        text, usage, session_id = parse_claude_json("{}")
        self.assertEqual(text, "")
        self.assertIsNone(session_id)
        self.assertEqual(usage.tokens_in, 0)
        self.assertEqual(usage.source, "unknown")
        self.assertFalse(usage.metered)

    def test_unusable_output_never_raises(self):
        for stdout in ("", "segfault", "[]", "null", '{"usage": "kapot"}'):
            with self.subTest(stdout=stdout):
                text, usage, session_id = parse_claude_json(stdout)
                self.assertEqual((text, session_id), ("", None))
                self.assertEqual(usage.cost_usd, 0.0)

    def test_picks_the_last_json_object_out_of_stream_noise(self):
        noisy = f"waarschuwing: iets\n{claude_stdout()}\n"
        text, usage, _ = parse_claude_json(noisy)
        self.assertIn("RUNRESULT", text)
        self.assertEqual(usage.turns, 12)


class ClaudeRunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = fake_config(self.root)
        self.calls: list[list[str]] = []

    def patch_run(self, *, stdout: str = "", timed_out: bool = False, returncode: int = 0):
        def fake(cmd, **kwargs):
            self.calls.append(list(cmd))
            return make_process(stdout, timed_out=timed_out, returncode=returncode)

        patcher = mock.patch.object(claude_runner, "run_process", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)


class RunTests(ClaudeRunnerTestCase):
    def test_a_complete_runresult_becomes_a_finished_run(self):
        self.patch_run(stdout=claude_stdout())
        result = ClaudeRunner(self.cfg).run(make_request())
        self.assertEqual(result.uitkomst, "klaar")
        self.assertEqual(result.dod, "6/6")
        self.assertEqual(result.summary_md, "Vier weken bouwlogboek toegevoegd.")
        self.assertEqual([a.type for a in result.artifacts], ["test"])
        self.assertEqual(result.session_id, "sess-1")
        self.assertIsNone(result.error)

    def test_a_missing_runresult_is_a_failed_run(self):
        self.patch_run(stdout=claude_stdout(result="Klaar hoor, alles gedaan."))
        result = ClaudeRunner(self.cfg).run(make_request())
        self.assertEqual(result.uitkomst, "mislukt")
        self.assertEqual(result.error, "geen RUNRESULT-blok")

    def test_a_timeout_becomes_afgebroken(self):
        self.patch_run(timed_out=True)
        result = ClaudeRunner(self.cfg).run(make_request(timeout_s=1))
        self.assertEqual(result.uitkomst, "afgebroken")
        self.assertIn("tijdslimiet", result.error)
        self.assertFalse(result.usage.metered)

    def test_the_raw_output_lands_under_the_state_directory(self):
        self.patch_run(stdout=claude_stdout())
        result = ClaudeRunner(self.cfg).run(make_request())
        self.assertEqual(result.raw_log_path, self.cfg.state_dir / "runs" / "3f9a2c")
        self.assertTrue((result.raw_log_path / "stdout.json").exists())

    def test_a_dry_run_never_starts_the_model(self):
        self.patch_run(stdout=claude_stdout())
        result = ClaudeRunner(self.cfg).run(make_request(dry_run=True))
        self.assertEqual(result.uitkomst, "afgebroken")
        self.assertIn("droogdraai", result.error)
        self.assertEqual(self.calls, [])

    def test_a_role_that_needs_a_worktree_without_a_repo_fails_before_running(self):
        self.patch_run(stdout=claude_stdout())
        result = ClaudeRunner(self.cfg).run(make_request(needs_worktree=True, repo=None))
        self.assertEqual(result.uitkomst, "mislukt")
        self.assertIn("geen repo", result.error)
        self.assertEqual(self.calls, [])

    def test_the_error_output_is_added_when_the_cli_itself_failed(self):
        self.patch_run(stdout="", returncode=1)
        result = ClaudeRunner(self.cfg).run(make_request())
        self.assertEqual(result.uitkomst, "mislukt")
        self.assertEqual(result.error, "geen RUNRESULT-blok")


class PermissionFlagTests(ClaudeRunnerTestCase):
    """De gevaarlijke vlag mag alleen in de gecontroleerde werkmap."""

    def test_absent_when_the_working_directory_is_not_a_sandboxed_worktree(self):
        self.patch_run(stdout=claude_stdout())
        ClaudeRunner(self.cfg).run(make_request(needs_worktree=False))
        self.assertNotIn(SKIP_PERMISSIONS, self.calls[0])

    def test_absent_for_a_repository_outside_the_allowlist(self):
        self.patch_run(stdout=claude_stdout())
        ClaudeRunner(self.cfg).run(make_request(repo="fightclub/towmotive-portal"))
        self.assertNotIn(SKIP_PERMISSIONS, self.calls[0])

    def test_present_inside_a_worktree_of_an_allowed_repository(self):
        cfg = git_sandbox(self.root, "raderwerk/agency-os")
        self.patch_run(stdout=claude_stdout())
        request = make_request(
            issue=make_issue(identifier="WV-157", title="Werkmapbeheer"),
            repo="raderwerk/agency-os",
            needs_worktree=True,
        )
        result = ClaudeRunner(cfg).run(request)
        self.assertEqual(result.uitkomst, "klaar")
        self.assertEqual(result.branch, "feat/WV-157-werkmapbeheer")
        self.assertIn(SKIP_PERMISSIONS, self.calls[0])
        self.assertEqual(self.calls[0][:6], [cfg.claude_bin, "-p", "--output-format", "json",
                                             "--model", "sonnet"])


class PullRequestTests(ClaudeRunnerTestCase):
    def test_a_finished_run_opens_a_pull_request(self):
        self.patch_run(stdout=claude_stdout())
        with mock.patch.object(
            ClaudeRunner, "_publish", return_value="https://github.com/raderwerk/x/pull/7"
        ):
            result = ClaudeRunner(self.cfg).run(make_request(needs_pr=True))
        self.assertEqual(result.uitkomst, "klaar")
        self.assertEqual(result.pr_url, "https://github.com/raderwerk/x/pull/7")

    def test_a_run_without_a_pull_request_is_not_finished(self):
        self.patch_run(stdout=claude_stdout())
        with mock.patch.object(
            ClaudeRunner, "_publish", side_effect=claude_runner.ExecutorError("push geweigerd")
        ):
            result = ClaudeRunner(self.cfg).run(make_request(needs_pr=True))
        self.assertEqual(result.uitkomst, "mislukt")
        self.assertIn("push geweigerd", result.error)


if __name__ == "__main__":
    unittest.main()
