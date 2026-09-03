"""Testhulpstukken van module A. Geen netwerk, geen echte klok.

`tests/fakes.py` is van module C en bestaat op deze branch nog niet; deze module
is de lokale variant waarmee A tegen zijn eigen contract kan bouwen zonder een
bestand van een andere eigenaar aan te raken.

`FakeLinearClient` erft van de echte `LinearClient` en vervangt alleen de
netwerklaag. Daardoor draaien alle schrijfsloten in de test net zo hard als in
productie: een test die per ongeluk `poort/akkoord` zet, valt om.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from agency_os.linear import queries
from agency_os.linear.client import LinearClient
from agency_os.linear.models import (
    AgentSessionView,
    Artifact,
    CommentView,
    IssueView,
    MutationRecord,
    RunRecord,
    issue_from_node,
)
from agency_os.linear.store import Store

DISPATCHER = "user-spil"
APPROVER = "user-mens"
T0 = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)


class _AnyKeyMap(dict):
    """Elke naam bestaat, met een voorspelbaar id. Onbekende labels test de echte client."""

    def __missing__(self, key: str) -> str:
        return f"id-{key}"

    def __contains__(self, key: object) -> bool:
        return True


def raw_issue(**overrides: Any) -> dict:
    """Een ruwe Linear-node in de vorm van WV-207 (bladlabels met parent)."""
    node: dict[str, Any] = {
        "id": "issue-207",
        "identifier": "WV-207",
        "title": "Publiek bouwlogboek, wekelijks",
        "description": (
            "## Doel\nEen wekelijks bouwlogboek.\n\n"
            "## Opdrachtcontract\n"
            "```yaml\n"
            "contract: v1\n"
            "klant: raderwerk\n"
            "repo: raderwerk/raderwerk-content\n"
            "basisbranch: main\n"
            "omgeving: preview\n"
            "publiek: true\n"
            "```\n"
        ),
        "url": "https://linear.app/fightclub-techhub/issue/WV-207",
        "priority": 2,
        "estimate": 2,
        "updatedAt": "2026-09-03T08:00:00.000Z",
        "team": {"key": "WV"},
        "state": {"id": "state-ingepland", "name": "Ingepland", "type": "unstarted"},
        "project": {"id": "proj-1", "name": "P8"},
        "assignee": None,
        "delegate": None,
        "labels": {"nodes": [
            {"id": "l1", "name": "content", "parent": {"name": "dienst"}},
            {"id": "l2", "name": "contentstuk", "parent": {"name": "soort"}},
            {"id": "l3", "name": "raderwerk", "parent": {"name": "klant"}},
            {"id": "l4", "name": "sonnet", "parent": {"name": "agent"}},
            {"id": "l5", "name": "raderwerk/raderwerk-content", "parent": {"name": "repo"}},
            {"id": "l6", "name": "risico-publiek", "parent": None},
        ]},
    }
    node.update(overrides)
    return node


def make_issue(**overrides: Any) -> IssueView:
    """Een IssueView, standaard in de vorm van WV-207."""
    view = issue_from_node(raw_issue())
    if "labels" in overrides:
        labels = tuple(sorted(overrides.pop("labels")))
        view = dataclasses.replace(
            view, labels=labels, label_ids={name: f"id-{name}" for name in labels})
    return dataclasses.replace(view, **overrides) if overrides else view


def make_comment(**overrides: Any) -> CommentView:
    base = dict(id="c-1", body="Een gewone opmerking.", created_at=T0,
                author_id=APPROVER, author_name="Youp", author_is_app=False)
    base.update(overrides)
    return CommentView(**base)


def make_run(**overrides: Any) -> RunRecord:
    base = dict(
        run_id="3f9a2c", issue_id="issue-207", issue_identifier="WV-207", team_key="WV",
        rol="redacteur", model="claude-sonnet-5", executor="claude", klant="raderwerk",
        dienst="content", gestart=T0, geeindigd=T0 + timedelta(seconds=761), duur_s=761.0,
        beurten=38, tokens_in=184203, tokens_uit=12044, cache_lees=902110, kosten_usd=4.21,
        kosten_eur=3.63, dod="6/6", uitkomst="klaar", volgende_status="Agentreview",
        pr_url="https://github.com/raderwerk/raderwerk-content/pull/7",
        artefacten=(Artifact("pr", "https://github.com/raderwerk/raderwerk-content/pull/7",
                             "PR #7"),),
        metered=True,
    )
    base.update(overrides)
    return RunRecord(**base)


def make_session(**overrides: Any) -> AgentSessionView:
    base = dict(id="sess-1", status="complete", summary=None, app_user_id="app-codex",
                app_user_name="Codex", created_at=T0, updated_at=T0, activities=(),
                pull_request_url=None)
    base.update(overrides)
    return AgentSessionView(**base)


def temp_store(path: str) -> Store:
    return Store(path)


class FakeLinearClient(LinearClient):
    """De echte client met de netwerklaag eruit en een geheugen ervoor in de plaats."""

    def __init__(self, issues: Sequence[IssueView] = (), *,
                 comments: Optional[Mapping[str, Sequence[CommentView]]] = None,
                 sessions: Optional[Mapping[str, Sequence[AgentSessionView]]] = None,
                 history: Optional[Mapping[str, Sequence[dict]]] = None,
                 history_supported: bool = True, dry_run: bool = False,
                 sinks: Sequence[Any] = (), issue_count: int = 84,
                 raw_nodes: Sequence[dict] = (), clock=None) -> None:
        super().__init__("test-key", dispatcher_user_id=DISPATCHER, sinks=sinks, dry_run=dry_run)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.raw_nodes: list[dict] = list(raw_nodes)
        self.issues: dict[str, IssueView] = {}
        for node in self.raw_nodes:
            view = self.to_issue_view(node)
            self.issues[view.id] = view
        for issue in issues:
            self.issues[issue.id] = issue
        self._comments: dict[str, list[CommentView]] = {
            k: list(v) for k, v in (comments or {}).items()}
        self._sessions: dict[str, list[AgentSessionView]] = {
            k: list(v) for k, v in (sessions or {}).items()}
        self._history: dict[str, list[dict]] = {k: list(v) for k, v in (history or {}).items()}
        self.history_supported = history_supported
        self.mutations: list[MutationRecord] = []
        self.issue_count = issue_count
        self._seq = 0
        self._failures: dict[str, Exception] = {}

    # -- geen netwerk --

    def _post(self, document: str, variables: Mapping[str, Any]) -> str:  # pragma: no cover
        raise AssertionError("een test mag nooit het netwerk op")

    def fail_next(self, mutation_name: str, exc: Exception) -> None:
        self._failures[mutation_name] = exc

    # -- reads --

    def _find(self, key: str) -> Optional[IssueView]:
        if key in self.issues:
            return self.issues[key]
        return next((i for i in self.issues.values() if i.identifier == key), None)

    def issue(self, identifier_or_id: str) -> IssueView:
        found = self._find(identifier_or_id)
        if found is None:
            raise KeyError(identifier_or_id)
        return found

    def comments(self, issue_id: str, *, limit: int = 50) -> list[CommentView]:
        return list(self._comments.get(issue_id, []))[:limit]

    def agent_sessions(self, issue_id: str) -> list[AgentSessionView]:
        return list(self._sessions.get(issue_id, []))

    def issue_history(self, issue_id: str, *, limit: int = 50) -> Optional[list[dict]]:
        if not self.history_supported:
            return None
        return list(self._history.get(issue_id, []))

    def label_ids(self) -> Mapping[str, str]:
        return _AnyKeyMap()

    def state_ids(self, team_key: str) -> Mapping[str, str]:
        return _AnyKeyMap()

    def organization_issue_count(self) -> int:
        return self.issue_count

    def query(self, document: str, variables: Optional[dict] = None) -> dict:
        variables = variables or {}
        if "IssueContext" in document:
            issue = self._find(str(variables.get("id")))
            if issue is None:
                return {"issue": None}
            return {"issue": {"id": issue.id, "team": {"key": issue.team_key},
                              "state": {"name": issue.state_name}}}
        if document is queries.POLL or "query Poll" in document:
            return {
                "organization": {"createdIssueCount": self.issue_count},
                "issues": {"nodes": list(self.raw_nodes),
                           "pageInfo": {"hasNextPage": False, "endCursor": None}},
            }
        raise AssertionError(f"onverwacht document in een test: {document[:60]}")

    # -- writes --

    def _write(self, document: str, variables: Mapping[str, Any], *, run_id: Optional[str],
               mutation: str, entity_id: str, summary: Mapping[str, Any],
               result_path: Sequence[str]) -> Optional[str]:
        if mutation in self._failures:
            raise self._failures.pop(mutation)
        self._seq += 1
        result_id = f"fake-{mutation}-{self._seq}"
        record = MutationRecord(
            at=self.clock(), run_id=run_id, mutation=mutation,
            entity_id=entity_id, variables_digest=f"digest-{self._seq}",
            variables_summary=dict(summary), result_id=result_id, ok=True, error=None,
            dry_run=self.dry_run,
        )
        self.mutations.append(record)
        self._emit(record)
        self._apply(mutation, entity_id, variables, summary, result_id)
        return result_id

    def _apply(self, mutation: str, entity_id: str, variables: Mapping[str, Any],
               summary: Mapping[str, Any], result_id: str) -> None:
        """Laat het geheugen de mutatie zien, zodat een tweede lezing klopt."""
        issue = self.issues.get(entity_id)
        if mutation == "commentCreate":
            body = (variables.get("input") or {}).get("body", "")
            self._comments.setdefault(entity_id, []).append(CommentView(
                id=result_id, body=body, created_at=self.clock(),
                author_id=DISPATCHER, author_name="Spil", author_is_app=True))
            return
        if mutation != "issueUpdate" or issue is None:
            return
        labels = set(issue.labels)
        labels.difference_update(summary.get("removedLabelIds") or [])
        labels.update(summary.get("addedLabelIds") or [])
        changes: dict[str, Any] = {"labels": tuple(sorted(labels))}
        state = summary.get("stateId")
        if isinstance(state, str) and state.startswith("<"):
            changes["state_name"] = state[1:-1]
        if "assigneeId" in summary:
            changes["assignee_id"] = summary["assigneeId"]
        if "delegateId" in summary:
            changes["delegate_id"] = summary["delegateId"]
        self.issues[entity_id] = dataclasses.replace(issue, **changes)


class RecordingSink:
    """Een MutationSink die alles bewaart."""

    def __init__(self) -> None:
        self.records: list[MutationRecord] = []

    def record(self, m: MutationRecord) -> None:
        self.records.append(m)
