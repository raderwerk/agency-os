"""Branchnamen, hergebruik van een eerdere branch, en de geweigerde push."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agency_os.executors.base import UnsafeWorktree
from agency_os.executors.worktree import (
    PushRefused,
    Worktree,
    branch_name,
    ensure_detached_worktree,
    ensure_worktree,
    find_existing_branch,
    has_commits_ahead,
    push_branch,
    remove_worktree,
    repo_dir,
    slugify,
)
from tests.executor_fakes import commit_file, git_sandbox

REPO = "raderwerk/agency-os"


class SlugTests(unittest.TestCase):
    def test_cuts_the_title_at_the_first_separator(self):
        self.assertEqual(slugify("Publiek bouwlogboek, wekelijks"), "publiek-bouwlogboek")

    def test_keeps_at_most_four_words(self):
        self.assertEqual(slugify("een twee drie vier vijf zes"), "een-twee-drie-vier")

    def test_strips_accents_punctuation_and_case(self):
        self.assertEqual(slugify("Café Zoutkaap ERP!"), "cafe-zoutkaap-erp")

    def test_a_dash_clause_is_a_separator_too(self):
        self.assertEqual(slugify("Kostenboek — dagafsluiting"), "kostenboek")

    def test_empty_title_yields_an_empty_slug(self):
        self.assertEqual(slugify(""), "")
        self.assertEqual(slugify("!!!"), "")

    def test_branch_name_follows_the_repo_convention(self):
        self.assertEqual(
            branch_name("WV-207", "Publiek bouwlogboek, wekelijks"),
            "feat/WV-207-publiek-bouwlogboek",
        )

    def test_branch_name_falls_back_when_the_title_has_no_words(self):
        self.assertEqual(branch_name("WV-207", "***"), "feat/WV-207-taak")


class WorktreeTests(unittest.TestCase):
    """Draait tegen een echte git-repository in een tijdelijke map."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("git") is None:  # pragma: no cover - git hoort er te zijn
            raise unittest.SkipTest("git ontbreekt")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = git_sandbox(Path(self.tmp.name), REPO)
        self.clone = repo_dir(self.cfg, REPO)

    def test_creates_a_worktree_on_a_new_feature_branch(self):
        worktree = ensure_worktree(self.cfg, REPO, "WV-207", "Publiek bouwlogboek", "main")
        self.assertTrue(worktree.created)
        self.assertEqual(worktree.branch, "feat/WV-207-publiek-bouwlogboek")
        self.assertTrue((worktree.path / "README.md").exists())
        self.assertTrue(worktree.path.is_relative_to(self.cfg.worktree_root))

    def test_a_second_call_reuses_the_same_worktree(self):
        first = ensure_worktree(self.cfg, REPO, "WV-207", "Publiek bouwlogboek", "main")
        second = ensure_worktree(self.cfg, REPO, "WV-207", "Andere titel", "main")
        self.assertFalse(second.created)
        self.assertEqual(second.path, first.path)
        self.assertEqual(second.branch, first.branch)

    def test_reuses_an_existing_branch_from_an_earlier_run(self):
        subprocess.run(
            ["git", "branch", "feat/WV-208-eerdere-poging"],
            cwd=self.clone, check=True, capture_output=True,
        )
        self.assertEqual(
            find_existing_branch(self.cfg, REPO, "WV-208"), "feat/WV-208-eerdere-poging"
        )
        worktree = ensure_worktree(self.cfg, REPO, "WV-208", "Heel andere titel", "main")
        self.assertEqual(worktree.branch, "feat/WV-208-eerdere-poging")

    def test_no_earlier_branch_means_no_probe_hit(self):
        self.assertIsNone(find_existing_branch(self.cfg, REPO, "WV-999"))

    def test_refuses_a_repo_outside_the_allowlist(self):
        with self.assertRaises(UnsafeWorktree):
            ensure_worktree(self.cfg, "fightclub/towmotive-portal", "WV-207", "Titel", "main")

    def test_dry_run_plans_the_worktree_without_creating_it(self):
        cfg = replace(self.cfg, dry_run=True)
        worktree = ensure_worktree(cfg, REPO, "WV-207", "Publiek bouwlogboek", "main")
        self.assertFalse(worktree.created)
        self.assertFalse(worktree.path.exists())

    def test_push_reaches_origin_and_reports_commits_ahead(self):
        worktree = ensure_worktree(self.cfg, REPO, "WV-207", "Publiek bouwlogboek", "main")
        self.assertFalse(has_commits_ahead(self.cfg, worktree))
        commit_file(worktree.path, "logboek.md", "week 1\n")
        self.assertTrue(has_commits_ahead(self.cfg, worktree))

        push_branch(self.cfg, worktree)
        remote = subprocess.run(
            ["git", "branch", "--list", worktree.branch],
            cwd=self.clone / ".." / ".." / "origin" / "agency-os.git",
            check=True, capture_output=True, text=True,
        )
        self.assertIn(worktree.branch, remote.stdout)

    def test_removing_a_worktree_keeps_the_branch(self):
        worktree = ensure_worktree(self.cfg, REPO, "WV-207", "Publiek bouwlogboek", "main")
        remove_worktree(self.cfg, worktree)
        self.assertFalse(worktree.path.exists())
        self.assertEqual(
            find_existing_branch(self.cfg, REPO, "WV-207"), "feat/WV-207-publiek-bouwlogboek"
        )

    def test_a_detached_review_checkout_has_no_branch_to_push(self):
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.clone, check=True, capture_output=True, text=True
        ).stdout.strip()
        worktree = ensure_detached_worktree(self.cfg, REPO, "WV-207", head)
        self.assertEqual(worktree.branch, "")
        self.assertEqual(worktree.head_sha, head)
        with self.assertRaises(PushRefused):
            push_branch(self.cfg, worktree)


class PushRefusalTests(unittest.TestCase):
    """De weigering hangt niet aan git: hij zit vóór het commando."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = git_sandbox(Path(self.tmp.name), REPO)

    def _worktree(self, branch: str, base: str = "main") -> Worktree:
        path = self.cfg.worktree_root / "agency-os" / "WV-207"
        return Worktree(REPO, path, branch, base, created=False, head_sha="deadbeef")

    def test_refuses_a_push_to_the_base_branch(self):
        with self.assertRaises(PushRefused):
            push_branch(self.cfg, self._worktree("main"))

    def test_refuses_a_push_to_any_protected_branch(self):
        for branch in ("master", "production", "staging"):
            with self.assertRaises(PushRefused):
                push_branch(self.cfg, self._worktree(branch, base="develop"))


if __name__ == "__main__":
    unittest.main()
