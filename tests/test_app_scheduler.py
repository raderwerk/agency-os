"""De cyclus: poorten eerst, dan claimen, dan draaien, dan de drie schrijfacties."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.fakes import FakeClient, FakeExecutor, issue_from_raw, load_fixture, make_issue, temp_store

from agency_os.app import scheduler
from agency_os.app.config import Config
from agency_os.app.logbook import Logbook
from agency_os.app.routing import load_table
from agency_os.app.scheduler import Context
from agency_os.executors.base import ExecutionResult, Usage
from agency_os.linear.models import Artifact
from agency_os.linear.poll import PollConfig

APPROVER = "user-mens"
DISPATCHER = "user-spil"
ENV = {
    "SPIL_LINEAR_API_KEY": "lin_api_test",
    "SPIL_DISPATCHER_USER_ID": DISPATCHER,
    "SPIL_APPROVER_IDS": APPROVER,
    "SPIL_FX_USD_EUR": "0.86",
    "SPIL_FX_SOURCE": "ECB",
    "SPIL_FX_DATE": "2026-09-02",
    "SPIL_CLAIM_SETTLE_S": "0",
}


def a_result(**overrides) -> ExecutionResult:
    now = datetime.now(timezone.utc)
    defaults = dict(
        run_id="ignored",
        uitkomst="klaar",
        summary_md="Vier weken bouwlogboek toegevoegd.",
        dod="2/2",
        question=None,
        error=None,
        pr_url="https://github.com/raderwerk/raderwerk-content/pull/7",
        branch="feat/WV-207-publiek-bouwlogboek",
        artifacts=(Artifact("pr", "https://github.com/raderwerk/raderwerk-content/pull/7", "PR #7"),),
        usage=Usage(tokens_in=1000, tokens_out=200, turns=3, duration_s=12.0, source="claude-json"),
        started_at=now,
        ended_at=now,
        session_id="sess-1",
        raw_log_path=None,
    )
    defaults.update(overrides)
    return ExecutionResult(**defaults)


class CycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env_file = Path(self.tmp.name) / "spil.env"
        env_file.write_text("\n".join(f"{k}={v}" for k, v in ENV.items()), encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {"SPIL_CONFIG_FILE": str(env_file), "SPIL_STATE_DIR": str(Path(self.tmp.name) / "state")},
            clear=True,
        ):
            self.cfg = Config.load()

    def context(self, client: FakeClient, executor=None, **cfg_changes) -> Context:
        cfg = self.cfg.with_overrides(**cfg_changes) if cfg_changes else self.cfg
        return Context(
            cfg=cfg,
            client=client,
            store=temp_store(self.tmp.name),
            table=load_table(),
            logbook=Logbook(cfg.logbook_dir),
            executors={"claude": executor or FakeExecutor(a_result())},
            poll_cfg=PollConfig(
                team_keys=("WV", "KR"),
                panel_identifier=cfg.panel_identifier,
                in_scope_states=scheduler.IN_SCOPE_STATES,
                max_claims=cfg.max_claims_per_cycle,
            ),
        )

    @staticmethod
    def only(client: FakeClient, identifier: str, *, dry_run: bool = False) -> FakeClient:
        """Hetzelfde bord, maar met één werkissue erop naast het bedieningspaneel."""
        keep = [issue for issue in client.all_issues() if issue.identifier in {identifier, "WV-156"}]
        return FakeClient(keep, dispatcher_user_id=DISPATCHER, dry_run=dry_run)

    @staticmethod
    def names(client: FakeClient) -> list[str]:
        return [m.mutation for m in client.mutations]


class HappyPathTest(CycleTestCase):
    def test_one_issue_is_claimed_run_and_written_back(self):
        client = self.only(FakeClient(dispatcher_user_id=DISPATCHER), "WV-207")
        executor = FakeExecutor(a_result())
        ctx = self.context(client, executor)

        report = scheduler.run_cycle(ctx, 1)

        self.assertFalse(report.halted)
        self.assertEqual((), report.errors)
        self.assertEqual(1, report.claimed)
        self.assertEqual(1, report.finished)

        issue = client.issue("WV-207")
        self.assertEqual("Agentreview", issue.state_name)
        self.assertIn("run/klaar", issue.labels)
        self.assertNotIn("run/bezet", issue.labels)

        self.assertEqual(
            ["commentCreate", "issueUpdate", "issueUpdate", "commentCreate",
             "issueUpdate", "attachmentLinkURL", "issueUpdate"],
            self.names(client),
            "claim (comment + label), status naar In uitvoering, dan comment -> issueUpdate -> bijlage -> vrijgave",
        )

    def test_the_prompt_carries_the_skeleton_the_role_and_the_output_contract(self):
        client = self.only(FakeClient(dispatcher_user_id=DISPATCHER), "WV-207")
        executor = FakeExecutor(a_result())
        scheduler.run_cycle(self.context(client, executor), 1)

        prompt = executor.requests[0].prompt
        self.assertIn("Onwrikbare regels", prompt)
        self.assertIn("# Redacteur", prompt)
        self.assertIn("json RUNRESULT", prompt)
        self.assertIn("WV-207", prompt)
        self.assertTrue(executor.requests[0].branch.startswith("feat/WV-207-"), executor.requests[0].branch)
        self.assertEqual("raderwerk/raderwerk-content", executor.requests[0].repo)

    def test_a_question_parks_the_issue_with_a_human(self):
        client = self.only(FakeClient(dispatcher_user_id=DISPATCHER), "WV-207")
        result = a_result(uitkomst="vraag", question="Welke vier weken precies?", pr_url=None, artifacts=())
        scheduler.run_cycle(self.context(client, FakeExecutor(result)), 1)

        issue = client.issue("WV-207")
        self.assertEqual("Wacht op input", issue.state_name)
        self.assertIn("schakelaar/mens-vereist", issue.labels)
        self.assertEqual(APPROVER, issue.assignee_id)

    def test_a_crashing_executor_becomes_a_failed_run_not_a_crashing_cycle(self):
        client = self.only(FakeClient(dispatcher_user_id=DISPATCHER), "WV-207")
        ctx = self.context(client, FakeExecutor(RuntimeError("claude viel om")))

        report = scheduler.run_cycle(ctx, 1)

        self.assertEqual((), report.errors)
        issue = client.issue("WV-207")
        self.assertEqual("In uitvoering", issue.state_name, "blijft staan waar hij stond")
        self.assertIn("run/mislukt", issue.labels)
        self.assertEqual(1, len(ctx.store.runs_on(datetime.now(timezone.utc).date())), "ook een mislukking is een regel")

    def test_dry_run_touches_nothing(self):
        client = self.only(FakeClient(dispatcher_user_id=DISPATCHER), "WV-207", dry_run=True)
        scheduler.run_cycle(self.context(client, FakeExecutor(a_result()), dry_run=True), 1)

        self.assertEqual("Ingepland", client.issue("WV-207").state_name)
        self.assertTrue(client.mutations, "de bedoelde mutaties worden wel vastgelegd")
        self.assertTrue(all(m.dry_run for m in client.mutations))


class GateFirstTest(CycleTestCase):
    def approved_client(self, body: str = "AKKOORD", *, author: str = APPROVER, is_app: bool = False) -> FakeClient:
        client = FakeClient(dispatcher_user_id=DISPATCHER)
        card_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        gate_issue = client.issue("WV-208")
        client.add_comment(gate_issue.id, "**Poortkaart · Merge of publicatie** — WV-208",
                           author_id=DISPATCHER, author_name="Spil", created_at=card_at)
        client.add_comment(gate_issue.id, body, author_id=author, author_name="Youp",
                           author_is_app=is_app, created_at=card_at + timedelta(minutes=5))
        return client

    def test_a_valid_akkoord_moves_the_issue_before_anything_is_claimed(self):
        client = self.approved_client()
        report = scheduler.run_cycle(self.context(client), 1)

        self.assertEqual(1, report.gates_seen)
        self.assertEqual(1, report.gates_applied)
        self.assertEqual("Na-merge controle", client.issue("WV-208").state_name)

        gate_writes = [i for i, m in enumerate(client.mutations) if m.entity_id == client.issue("WV-208").id]
        claim_writes = [i for i, m in enumerate(client.mutations) if m.entity_id == client.issue("WV-207").id]
        self.assertTrue(gate_writes and claim_writes)
        self.assertLess(max(gate_writes), min(claim_writes), "poortbeslissingen gaan vóór nieuw werk")

    def test_an_approval_by_an_app_user_stops_the_issue(self):
        client = self.approved_client(author="user-codex", is_app=True)
        report = scheduler.run_cycle(self.context(client), 1)

        self.assertEqual(0, report.gates_applied)
        issue = client.issue("WV-208")
        self.assertEqual("Poort · Merge of publicatie", issue.state_name)
        self.assertIn("run/onbevestigd", issue.labels)
        self.assertIn("schakelaar/mens-vereist", issue.labels)

    def test_an_approval_by_someone_who_is_not_an_approver_stops_the_issue(self):
        client = self.approved_client(author="user-vreemde")
        scheduler.run_cycle(self.context(client), 1)
        self.assertIn("run/onbevestigd", client.issue("WV-208").labels)

    def test_qa_hands_the_issue_to_a_human_through_the_six_gate_actions(self):
        qa_issue = issue_from_raw(load_fixture()[0])
        qa_issue = make_issue(state_name="QA op preview", labels=qa_issue.labels)
        client = FakeClient([qa_issue], dispatcher_user_id=DISPATCHER)

        scheduler.run_cycle(self.context(client), 1)

        issue = client.issue("WV-207")
        self.assertEqual("Poort · Merge of publicatie", issue.state_name)
        self.assertEqual(APPROVER, issue.assignee_id)
        self.assertIsNone(issue.delegate_id)
        self.assertEqual(1, issue.priority)
        self.assertIn("poort/wacht-op-mens", issue.labels)
        self.assertTrue(any("Poortkaart" in c.body for c in client.comments(issue.id)))
        self.assertTrue(client.attachments)


class KillSwitchTest(CycleTestCase):
    def test_pauze_alles_stops_the_cycle_and_writes_nothing_else(self):
        client = FakeClient(dispatcher_user_id=DISPATCHER)
        panel = client.issue("WV-156")
        client.issues[panel.id] = make_issue(
            id=panel.id, identifier="WV-156", title=panel.title, description=panel.description,
            state_name="In uitvoering", labels=("schakelaar/pauze-alles",), contract=None,
        )
        report = scheduler.run_cycle(self.context(client), 1)

        self.assertTrue(report.halted)
        self.assertEqual(0, report.claimed)
        self.assertEqual([], self.names(client), "een gestopte cyclus claimt niets en schrijft niets")
        self.assertEqual("Ingepland", client.issue("WV-207").state_name)

    def test_a_paused_issue_is_skipped(self):
        client = self.only(FakeClient(dispatcher_user_id=DISPATCHER), "WV-207")
        issue = client.issue("WV-207")
        client.issues[issue.id] = make_issue(labels=tuple(sorted(issue.labels + ("schakelaar/pauze",))))

        report = scheduler.run_cycle(self.context(client), 1)
        self.assertEqual(0, report.claimed)
        self.assertEqual([], self.names(client))

    def test_the_loop_guard_pauses_the_issue_instead_of_running_it_twice(self):
        client = self.only(FakeClient(dispatcher_user_id=DISPATCHER), "WV-207")
        ctx = self.context(client)
        ctx.store.bump_role_run(client.issue("WV-207").id, "redacteur", datetime.now(timezone.utc).date())

        report = scheduler.run_cycle(ctx, 1)

        self.assertEqual(0, report.claimed)
        issue = client.issue("WV-207")
        self.assertIn("lus-verdacht", issue.labels)
        self.assertIn("schakelaar/pauze", issue.labels)


if __name__ == "__main__":
    unittest.main()
