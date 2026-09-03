"""Contracten, zandbakbewaking, procesgroep-kill en het RUNRESULT-blok."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from agency_os.executors import base
from agency_os.executors.base import (
    ExecutorConfig,
    UnsafeWorktree,
    assert_safe_worktree,
    build_executors,
)
from agency_os.executors.claude_runner import RunResult, parse_runresult
from agency_os.executors.process import run_process
from tests.executor_fakes import fake_config


class SafeWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = fake_config(Path(self.tmp.name))
        self.path = self.cfg.worktree_root / "agency-os" / "WV-207"

    def test_accepts_a_worktree_under_the_root_for_an_allowed_repo(self):
        assert_safe_worktree(self.path, "raderwerk/agency-os", self.cfg)

    def test_refuses_a_repo_outside_the_allowlist(self):
        with self.assertRaises(UnsafeWorktree):
            assert_safe_worktree(self.path, "fightclub/towmotive-portal", self.cfg)

    def test_refuses_a_path_outside_the_worktree_root(self):
        with self.assertRaises(UnsafeWorktree):
            assert_safe_worktree(Path(self.tmp.name) / "elders", "raderwerk/agency-os", self.cfg)

    def test_refuses_an_escape_through_dotdot(self):
        with self.assertRaises(UnsafeWorktree):
            assert_safe_worktree(self.path / ".." / ".." / "..", "raderwerk/agency-os", self.cfg)

    def test_refuses_a_forbidden_prefix_even_inside_the_worktree_root(self):
        cfg = ExecutorConfig(worktree_root=Path("/Users/youp/Developer/Fightclub/.worktrees"))
        with self.assertRaises(UnsafeWorktree):
            assert_safe_worktree(
                cfg.worktree_root / "agency-os" / "WV-207", "raderwerk/agency-os", cfg
            )


class BuildExecutorsTests(unittest.TestCase):
    def test_returns_the_four_lanes_under_their_route_names(self):
        executors = build_executors(ExecutorConfig())
        self.assertEqual(
            sorted(executors), ["claude", "codex-cli", "native-codex", "native-cursor"]
        )
        for key, executor in executors.items():
            self.assertEqual(executor.name, key)

    def test_sync_lanes_run_and_async_lanes_trigger_and_poll(self):
        executors = build_executors(ExecutorConfig())
        self.assertTrue(hasattr(executors["claude"], "run"))
        self.assertTrue(hasattr(executors["codex-cli"], "run"))
        for name in ("native-codex", "native-cursor"):
            self.assertTrue(hasattr(executors[name], "trigger"))
            self.assertTrue(hasattr(executors[name], "poll"))


class RunResultTests(unittest.TestCase):
    def test_reads_the_last_runresult_block(self):
        text = (
            "eerste poging\n```json RUNRESULT\n{\"uitkomst\": \"mislukt\"}\n```\n"
            "en daarna\n```json RUNRESULT\n{\"uitkomst\": \"klaar\", \"dod\": \"6/6\"}\n```\n"
        )
        self.assertEqual(parse_runresult(text)["dod"], "6/6")

    def test_missing_block_is_a_failed_run_and_never_a_guess(self):
        result = RunResult.from_dict(parse_runresult("Ik heb alles netjes afgerond, echt waar."))
        self.assertEqual(result.uitkomst, "mislukt")
        self.assertEqual(result.error, "geen RUNRESULT-blok")

    def test_broken_json_falls_back_to_an_earlier_valid_block(self):
        text = (
            "```json RUNRESULT\n{\"uitkomst\": \"klaar\"}\n```\n"
            "```json RUNRESULT\n{niet eens json}\n```\n"
        )
        self.assertEqual(RunResult.from_dict(parse_runresult(text)).uitkomst, "klaar")

    def test_unknown_outcome_is_refused(self):
        result = RunResult.from_dict({"uitkomst": "bijna-klaar"})
        self.assertEqual(result.uitkomst, "mislukt")
        self.assertIn("bijna-klaar", result.error)

    def test_question_without_text_is_refused(self):
        result = RunResult.from_dict({"uitkomst": "vraag", "vraag": None})
        self.assertEqual(result.uitkomst, "mislukt")

    def test_question_with_text_is_kept(self):
        result = RunResult.from_dict({"uitkomst": "vraag", "vraag": "Welke toon?"})
        self.assertEqual((result.uitkomst, result.vraag), ("vraag", "Welke toon?"))

    def test_evidence_without_url_is_dropped_and_unknown_types_become_document(self):
        result = RunResult.from_dict(
            {
                "uitkomst": "klaar",
                "bewijs": [
                    {"type": "pr", "url": "https://example.invalid/pr", "label": "PR #7"},
                    {"type": "gevoel", "url": "https://example.invalid/x"},
                    {"type": "test"},
                    "geen object",
                ],
            }
        )
        self.assertEqual([a.type for a in result.bewijs], ["pr", "document"])


class RunProcessTests(unittest.TestCase):
    def test_passes_stdin_and_captures_stdout(self):
        result = run_process(["/bin/cat"], stdin_text="hallo", timeout_s=30)
        self.assertEqual(result.stdout, "hallo")
        self.assertTrue(result.ok)

    def test_a_timeout_kills_the_whole_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "kleinkind.pid"
            result = run_process(
                ["/bin/sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait"],
                timeout_s=1.0,
            )
            self.assertTrue(result.timed_out)
            grandchild = int(pidfile.read_text().strip())

        for _ in range(30):
            if not _alive(grandchild):
                break
            time.sleep(0.1)
        self.assertFalse(_alive(grandchild), "het kleinkind draaide door na de tijdslimiet")

    def test_check_raises_on_a_failing_command(self):
        with self.assertRaises(base.CommandFailed):
            run_process(["/bin/sh", "-c", "echo stuk >&2; exit 3"], timeout_s=30).check()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


if __name__ == "__main__":
    unittest.main()
