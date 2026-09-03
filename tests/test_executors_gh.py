"""De gh-wrapper: PR terugvinden, merge-verificatie, en het ontbreken van merge."""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agency_os.executors import gh
from agency_os.executors.gh import find_pr_for_branch, open_pr, pr_diff, read_pr
from tests.executor_fakes import fake_config, make_process

REPO = "raderwerk/raderwerk-content"
BRANCH = "feat/WV-207-publiek-bouwlogboek"


def pr_node(**overrides) -> dict:
    node = {
        "number": 7,
        "url": f"https://github.com/{REPO}/pull/7",
        "state": "OPEN",
        "isDraft": False,
        "mergedAt": None,
        "mergedBy": None,
        "headRefOid": "a" * 40,
        "statusCheckRollup": [{"__typename": "CheckRun", "conclusion": "SUCCESS"}],
    }
    node.update(overrides)
    return node


class GhTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = fake_config(Path(self.tmp.name))
        self.calls: list[list[str]] = []

    def patch_gh(self, *payloads: str):
        """Vervang `run_process` door een stub die de opgegeven uitvoer teruggeeft."""
        outputs = list(payloads)

        def fake(cmd, **kwargs):
            self.calls.append(list(cmd))
            return make_process(outputs.pop(0) if outputs else "")

        patcher = mock.patch.object(gh, "run_process", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)


class FindPrTests(GhTestCase):
    def test_parses_the_pull_request(self):
        self.patch_gh(json.dumps([pr_node()]))
        pull_request = find_pr_for_branch(self.cfg, REPO, BRANCH)
        self.assertEqual(pull_request.number, 7)
        self.assertEqual(pull_request.checks_conclusion, "success")
        self.assertFalse(pull_request.merged)
        self.assertIn("--head", self.calls[0])

    def test_prefers_the_open_pull_request(self):
        self.patch_gh(
            json.dumps([pr_node(number=5, state="CLOSED"), pr_node(number=7, state="OPEN")])
        )
        self.assertEqual(find_pr_for_branch(self.cfg, REPO, BRANCH).number, 7)

    def test_no_pull_request_yet(self):
        self.patch_gh("[]")
        self.assertIsNone(find_pr_for_branch(self.cfg, REPO, BRANCH))

    def test_unreadable_output_is_not_a_crash(self):
        self.patch_gh("gh: kon niet inloggen")
        self.assertIsNone(find_pr_for_branch(self.cfg, REPO, BRANCH))


class MergeVerificationTests(GhTestCase):
    """Spec 7.5: een merge door een bot telt niet als menselijke goedkeuring."""

    def _read(self, merged_by: dict | None) -> gh.PullRequest:
        self.patch_gh(
            json.dumps(
                pr_node(state="MERGED", mergedAt="2026-09-03T09:00:00Z", mergedBy=merged_by)
            )
        )
        return read_pr(self.cfg, REPO, 7)

    def test_a_human_merge_is_recognised(self):
        pull_request = self._read({"login": "youpv", "is_bot": False, "type": "User"})
        self.assertTrue(pull_request.merged)
        self.assertEqual(pull_request.merged_by_login, "youpv")
        self.assertFalse(pull_request.merged_by_is_bot)

    def test_the_is_bot_flag_is_honoured(self):
        self.assertTrue(self._read({"login": "codex", "is_bot": True}).merged_by_is_bot)

    def test_a_bot_login_suffix_is_honoured(self):
        self.assertTrue(self._read({"login": "github-actions[bot]"}).merged_by_is_bot)

    def test_the_bot_type_is_honoured(self):
        self.assertTrue(self._read({"login": "raderwerk-app", "type": "Bot"}).merged_by_is_bot)

    def test_an_unmerged_pull_request_has_no_merger(self):
        self.patch_gh(json.dumps(pr_node()))
        pull_request = read_pr(self.cfg, REPO, 7)
        self.assertFalse(pull_request.merged)
        self.assertIsNone(pull_request.merged_by_is_bot)


class ChecksTests(GhTestCase):
    def _checks(self, rollup) -> str | None:
        self.patch_gh(json.dumps(pr_node(statusCheckRollup=rollup)))
        return read_pr(self.cfg, REPO, 7).checks_conclusion

    def test_a_failing_check_wins(self):
        self.assertEqual(
            self._checks([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]), "failure"
        )

    def test_a_running_check_is_pending(self):
        self.assertEqual(
            self._checks([{"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS"}]), "pending"
        )

    def test_all_green_is_success(self):
        self.assertEqual(self._checks([{"conclusion": "SUCCESS"}]), "success")

    def test_without_checks_there_is_no_conclusion(self):
        self.assertIsNone(self._checks([]))


class OpenPrTests(GhTestCase):
    def test_an_existing_pull_request_is_reused(self):
        self.patch_gh(json.dumps([pr_node()]))
        pull_request = open_pr(self.cfg, REPO, BRANCH, "main", "Titel", "Body")
        self.assertEqual(pull_request.number, 7)
        self.assertEqual(len(self.calls), 1, "een herstart mag geen tweede PR openen")

    def test_creates_and_then_reads_the_pull_request(self):
        self.patch_gh("[]", f"https://github.com/{REPO}/pull/7\n", json.dumps(pr_node()))
        pull_request = open_pr(self.cfg, REPO, BRANCH, "main", "Titel", "Body")
        self.assertEqual(pull_request.number, 7)
        self.assertIn("create", self.calls[1])

    def test_dry_run_never_calls_create(self):
        cfg = fake_config(Path(self.tmp.name), dry_run=True)
        self.patch_gh("[]")
        pull_request = open_pr(cfg, REPO, BRANCH, "main", "Titel", "Body")
        self.assertEqual(pull_request.state, "dry-run")
        self.assertEqual(len(self.calls), 1)


class DiffTests(GhTestCase):
    def test_the_diff_is_clipped_with_a_visible_marker(self):
        self.patch_gh("x" * 5000)
        diff = pr_diff(self.cfg, REPO, 7, max_bytes=100)
        self.assertIn("[diff afgekapt op 100 bytes]", diff)
        self.assertLess(len(diff), 200)

    def test_a_short_diff_is_returned_whole(self):
        self.patch_gh("diff --git a/x b/x\n")
        self.assertEqual(pr_diff(self.cfg, REPO, 7), "diff --git a/x b/x\n")


class NoMergeTests(unittest.TestCase):
    """Mergen is mensenwerk. Dat is hier een ontbrekende tak, geen belofte."""

    def test_the_module_has_no_merge_function(self):
        self.assertFalse(hasattr(gh, "merge"))
        self.assertEqual([name for name in dir(gh) if name.startswith("merge")], [])

    def test_no_gh_command_in_this_module_says_merge(self):
        source = inspect.getsource(gh)
        self.assertNotIn('"merge"', source)
        self.assertNotIn("'merge'", source)


if __name__ == "__main__":
    unittest.main()
