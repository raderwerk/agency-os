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
from agency_os.linear import store as store_module

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


class ReviewerFamilyTest(unittest.TestCase):
    """agent-roster.md sectie 3: de reviewer is altijd een andere modelfamilie."""

    def test_the_reviewer_is_not_the_family_that_did_the_building(self):
        reviewer = routing.MODELS[TABLE.roles["reviewer"].default_model]
        for maker in ("redacteur", "ontwikkelaar", "ontwerper", "campagneplanner"):
            with self.subTest(rol=maker):
                self.assertNotEqual(
                    reviewer.family, routing.MODELS[TABLE.roles[maker].default_model].family,
                    f"{maker} en de reviewer draaien op dezelfde familie; dat is precies de "
                    "kwaliteitsmaatregel die de roster niet wil verliezen")

    def test_an_agent_label_on_the_issue_cannot_pick_the_reviewer(self):
        """`agent/<model>` zegt wie het werk maakt, niet wie het beoordeelt.

        WV-210 draagt `agent/fable`. Zonder deze regel zette dat label de
        reviewer op de Claude-familie -- dezelfde familie als de bouwer -- terwijl
        de laan `codex-cli` bleef. Er draaide dan GPT-5.6 terwijl het Kostenboek
        `claude-opus-5` boekte tegen de prijs van Opus.
        """
        for label, verwacht in (("agent/fable", "codex-cli"), ("agent/opus", "codex-cli"),
                                ("agent/codex", "codex-cli"), ("agent/cursor", "codex-cli")):
            with self.subTest(label=label):
                issue = make_issue(state_name="Agentreview", labels=("dienst/content", label))
                route = routing.decide(TABLE, issue, allow_fable=True)
                self.assertEqual(route.role.key, "reviewer")
                self.assertEqual(route.model_key, verwacht)
                self.assertEqual(route.executor_name, "codex-cli")
                self.assertIn("genegeerd", route.reason)

    def test_a_building_role_still_follows_the_agent_label(self):
        issue = make_issue(state_name="Ingepland",
                           labels=("soort/contentstuk", "agent/fable"))
        route = routing.decide(TABLE, issue, allow_fable=True)
        self.assertEqual(route.role.key, "redacteur")
        self.assertEqual(route.model_key, "fable")

    def test_the_reviewer_lane_finishes_inside_the_same_cycle(self):
        self.assertNotIn(TABLE.roles["reviewer"].default_model, routing.NATIVE_MODELS,
                         "een cloudsessie levert haar oordeel pas cycli later")


class LoopGuardTest(unittest.TestCase):
    """Spec 8.6: drie runs van dezelfde rol op één issue per dag, niet één."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = temp_store(tmp.name)
        self.issue = make_issue()
        self.day = date(2026, 9, 3)

    def test_the_fourth_run_of_the_same_role_on_one_day_is_refused(self):
        for _ in range(routing.MAX_ROLE_RUNS_PER_DAY):
            self.assertIsNone(loop_guard(self.store, self.issue, "redacteur", self.day))
            self.store.bump_role_run(self.issue.id, "redacteur", self.day)

        reason = loop_guard(self.store, self.issue, "redacteur", self.day)
        self.assertIsNotNone(reason)
        self.assertIn("redacteur", reason)
        self.assertIn("3x", reason)
        self.assertIsNone(loop_guard(self.store, self.issue, "reviewer", self.day),
                          "andere rol mag nog wel")
        self.assertIsNone(loop_guard(self.store, self.issue, "redacteur", date(2026, 9, 4)),
                          "morgen mag weer")

    def test_a_repair_round_fits_inside_one_day(self):
        """Bouwen, afgekeurd worden, herstellen: de lus die op 2026-09-03 niet paste."""
        for role in ("ontwikkelaar", "reviewer", "ontwikkelaar", "reviewer"):
            self.assertIsNone(loop_guard(self.store, self.issue, role, self.day), role)
            self.store.bump_role_run(self.issue.id, role, self.day)

    def test_a_lane_that_never_started_does_not_eat_a_turn(self):
        for _ in range(5):
            self.store.bump_role_run(self.issue.id, "reviewer", self.day)
            self.store.discount_role_run(self.issue.id, "reviewer", self.day)
        self.assertIsNone(loop_guard(self.store, self.issue, "reviewer", self.day))

    def test_the_refusal_names_the_attempts_that_never_started(self):
        for _ in range(routing.MAX_ROLE_RUNS_PER_DAY):
            self.store.bump_role_run(self.issue.id, "qa", self.day)
        self.store.bump_role_run(self.issue.id, "qa", self.day)
        self.store.discount_role_run(self.issue.id, "qa", self.day)

        reason = loop_guard(self.store, self.issue, "qa", self.day)
        self.assertIn("3x", reason)
        self.assertIn("1 poging(en) die niet startten", reason)

    def test_the_store_reports_loops_at_the_same_threshold(self):
        """Twee getallen op twee lagen; ze mogen niet uit elkaar lopen."""
        self.assertEqual(routing.MAX_ROLE_RUNS_PER_DAY, store_module.LOOP_LIMIT)


if __name__ == "__main__":
    unittest.main()
