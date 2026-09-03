"""Routering: elke regel uit routing.json, elke override, en de lusdetectie."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests.fakes import make_issue, temp_store

from agency_os.app import routing
from agency_os.app.routing import Refusal, Route, RoutingError, decide, load_table, loop_guard, resolve

TABLE = load_table()


def issue_for(team: str, state: str, **labels) -> object:
    """Een issue met precies de labels die een regel nodig heeft."""
    canonical = tuple(sorted(f"{group}/{value}" for group, value in labels.items() if value))
    return make_issue(team_key=team, state_name=state, labels=canonical, estimate=2, contract=None)


class RulesTest(unittest.TestCase):
    def test_every_rule_in_the_table_routes(self):
        cases = [
            ("WV", "Ingepland", {"soort": "contentstuk"}, "redacteur"),
            ("WV", "Ingepland", {"dienst": "web", "soort": "feature", "repo": "raderwerk/zoutkaap-shop"}, "ontwikkelaar"),
            ("WV", "Ingepland", {"dienst": "web", "soort": "bug", "repo": "raderwerk/zoutkaap-shop"}, "ontwikkelaar"),
            ("WV", "Ingepland", {"dienst": "web", "soort": "incident", "repo": "raderwerk/zoutkaap-shop"}, "ontwikkelaar"),
            ("WV", "Ingepland", {"soort": "designtaak"}, "ontwerper"),
            ("WV", "Ingepland", {"soort": "campagne"}, "campagneplanner"),
            ("WV", "Ingepland", {"soort": "socialkalender"}, "campagneplanner"),
            ("WV", "Ingepland", {"soort": "bureau"}, "ontwikkelaar"),
            ("WV", "Ingepland", {"soort": "onderzoek"}, "ontwikkelaar"),
            ("WV", "Agentreview", {}, "reviewer"),
            ("WV", "QA op preview", {}, "qa"),
            ("WV", "Na-merge controle", {}, "qa-rookproef"),
            ("KR", "Lead", {}, "account"),
            ("KR", "Discovery", {}, "strateeg"),
            ("KR", "Voorstel", {}, "strateeg"),
        ]
        for team, state, labels, expected in cases:
            with self.subTest(state=state, labels=labels):
                route = resolve(TABLE, issue_for(team, state, **labels), allow_fable=True)
                self.assertIsNotNone(route, f"{team}/{state} zou moeten routeren")
                self.assertEqual(expected, route.role.key)

    def test_first_matching_rule_wins(self):
        issue = issue_for("WV", "Ingepland", soort="contentstuk", dienst="web", repo="raderwerk/raderwerk-content")
        self.assertEqual("redacteur", resolve(TABLE, issue, allow_fable=True).role.key)

    def test_unknown_state_has_no_route(self):
        outcome = decide(TABLE, issue_for("WV", "Backlog", soort="contentstuk"), allow_fable=True)
        self.assertIsInstance(outcome, Refusal)
        self.assertEqual("geen-route", outcome.code)
        self.assertEqual("overslaan", outcome.action)
        self.assertIsNone(resolve(TABLE, issue_for("WV", "Backlog"), allow_fable=True))


class OverrideTest(unittest.TestCase):
    def test_agent_label_beats_the_default_model(self):
        issue = issue_for("WV", "Ingepland", soort="contentstuk", agent="opus")
        route = resolve(TABLE, issue, allow_fable=True)
        self.assertEqual("opus", route.model_key)
        self.assertIn("agent/opus", route.reason)

    def test_native_label_switches_the_executor(self):
        for hint, executor in (("codex", "native-codex"), ("cursor", "native-cursor")):
            with self.subTest(hint=hint):
                route = resolve(TABLE, issue_for("WV", "Ingepland", soort="contentstuk", agent=hint), allow_fable=True)
                self.assertEqual(executor, route.executor_name)
                self.assertEqual(hint, route.model_key)

    def test_fable_downgrade_is_written_into_the_reason(self):
        issue = issue_for("KR", "Lead")
        allowed = resolve(TABLE, issue, allow_fable=True)
        self.assertEqual("fable", allowed.model_key)

        downgraded = resolve(TABLE, issue, allow_fable=False)
        self.assertEqual("opus", downgraded.model_key)
        self.assertIn("afzwakking", downgraded.reason)

    def test_agent_mens_is_never_routed(self):
        issue = issue_for("WV", "Ingepland", soort="contentstuk", agent="mens")
        outcome = decide(TABLE, issue, allow_fable=True)
        self.assertIsInstance(outcome, Refusal)
        self.assertEqual("agent-mens", outcome.code)
        self.assertIsNone(resolve(TABLE, issue, allow_fable=True))

    def test_xl_goes_back_to_backlog(self):
        issue = make_issue(state_name="Ingepland", labels=("soort/contentstuk",), estimate=5, contract=None)
        outcome = decide(TABLE, issue, allow_fable=True)
        self.assertIsInstance(outcome, Refusal)
        self.assertEqual("xl", outcome.code)
        self.assertEqual("backlog", outcome.action)

    def test_web_without_a_repo_becomes_a_question(self):
        issue = make_issue(state_name="Ingepland", labels=("dienst/web", "soort/feature"), estimate=2, contract=None)
        outcome = decide(TABLE, issue, allow_fable=True)
        self.assertIsInstance(outcome, Refusal)
        self.assertEqual("geen-repo", outcome.code)
        self.assertEqual("vraag", outcome.action)

    def test_repo_from_the_contract_counts_as_a_repo(self):
        issue = make_issue(state_name="Ingepland", labels=("dienst/web", "soort/feature"), estimate=2)
        self.assertEqual("raderwerk/raderwerk-content", issue.repo)
        self.assertIsInstance(decide(TABLE, issue, allow_fable=True), Route)


class TableValidationTest(unittest.TestCase):
    def test_shipped_table_has_a_prompt_for_every_role(self):
        for role in TABLE.roles.values():
            self.assertTrue(role.prompt_path.is_file(), f"{role.key} mist {role.prompt_file}")
            self.assertIn(role.default_model, routing.MODELS)

    def test_unknown_role_in_a_rule_is_fatal(self):
        self._assert_invalid({"version": 1, "roles": {}, "rules": [{"team": "WV", "state": "X", "when": {}, "role": "spook"}]})

    def test_missing_prompt_file_is_fatal(self):
        table = json.loads(routing.ROUTING_TABLE_PATH.read_text(encoding="utf-8"))
        table["roles"]["redacteur"]["prompt_file"] = "bestaat-niet.md"
        self._assert_invalid(table)

    def _assert_invalid(self, table: dict) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "routing.json"
            path.write_text(json.dumps(table), encoding="utf-8")
            with self.assertRaises(RoutingError):
                load_table(path)


class LoopGuardTest(unittest.TestCase):
    def test_second_run_of_the_same_role_on_one_day_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = temp_store(tmp)
            issue = make_issue()
            day = date(2026, 9, 3)
            self.assertIsNone(loop_guard(store, issue, "redacteur", day))

            store.bump_role_run(issue.id, "redacteur", day)
            reason = loop_guard(store, issue, "redacteur", day)
            self.assertIsNotNone(reason)
            self.assertIn("redacteur", reason)
            self.assertIsNone(loop_guard(store, issue, "reviewer", day), "andere rol mag nog wel")
            self.assertIsNone(loop_guard(store, issue, "redacteur", date(2026, 9, 4)), "morgen mag weer")


if __name__ == "__main__":
    unittest.main()
