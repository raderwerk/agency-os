"""Gedeelde testdubbels. Eigendom van C, bevroren vanaf dag 1 (architectuur 3.8).

A en B bouwen hiertegen, dus de namen en handtekeningen in dit bestand veranderen
niet zonder diff-review van alle drie. `FakeClient` doet de lees- én schrijfkant
van `LinearClient` in het geheugen, houdt elke schrijfactie bij als
`MutationRecord`, en weigert dezelfde dingen als de echte client weigert -- een
testdubbel die wél door een poort laat, bewijst het verkeerde.

Eén afwijking van architectuur 3.8, met opzet: `comments` is hier een methode en
geen attribuut, want zo heet hij op `LinearClient` en dat is de kant die A en B
aanroepen. De opslag zit in `comments_by_issue`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from agency_os import gate
from agency_os.linear.client import WriteRefused
from agency_os.linear.models import (
    ActivityView,
    AgentSessionView,
    Artifact,
    CommentView,
    Contract,
    IssueView,
    MutationRecord,
    RunRecord,
    issue_from_node,
)
from agency_os.linear.store import Store

from tests.support_linear import refuse_exclusive_conflicts

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SUMMARY_KEYS = ("stateId", "addedLabelIds", "removedLabelIds", "assigneeId", "delegateId", "priority")
GATE_LABELS = ("poort/akkoord", "poort/afgekeurd")
UNREMOVABLE = "schakelaar/pauze-alles"


def canonical_label(node: Mapping[str, Any]) -> str:
    """Leaf + parent, zoals Linear ze teruggeeft (architectuur 3.1)."""
    parent = (node.get("parent") or {}).get("name")
    return f"{parent}/{node['name']}" if parent else node["name"]


def issue_from_raw(raw: Mapping[str, Any]) -> IssueView:
    """Zet een ruwe Linear-issue-node om in een IssueView.

    Zelfde omzetting als de poll gebruikt, dus de fakes kunnen niet afdrijven van
    de echte client: `agency_os.linear.models.issue_from_node`.
    """
    return issue_from_node(raw)


def node_from_issue(issue: IssueView) -> dict:
    """De omgekeerde weg: IssueView -> ruwe Linear-node.

    De echte `poll` leest via `paginate`, niet via `.issues`. Zonder deze stap
    zou de fake twee waarheden hebben -- een bevroren fixture aan de leeskant en
    een levende dict aan de schrijfkant -- en zouden testen groen blijven terwijl
    de dispatcher zijn eigen schrijfacties niet terugziet.
    """
    labels = []
    for name in issue.labels:
        parent, _, leaf = name.partition("/")
        node: dict[str, Any] = {"id": issue.label_ids.get(name, f"lab-{name}")}
        if leaf:
            node["name"] = leaf
            node["parent"] = {"name": parent}
        else:
            node["name"] = name
        labels.append(node)
    return {
        "id": issue.id,
        "identifier": issue.identifier,
        "title": issue.title,
        "description": issue.description,
        "url": issue.url,
        "team": {"key": issue.team_key},
        "state": {"id": issue.state_id, "name": issue.state_name, "type": issue.state_type},
        "estimate": issue.estimate,
        "priority": issue.priority,
        "labels": {"nodes": labels},
        "project": {"id": issue.project_id, "name": issue.project_name} if issue.project_id else None,
        "assignee": {"id": issue.assignee_id} if issue.assignee_id else None,
        "delegate": {"id": issue.delegate_id} if issue.delegate_id else None,
        "updatedAt": issue.updated_at.isoformat(),
    }


def load_fixture(name: str = "issues.json") -> list[dict]:
    """De ruwe antwoordvorm uit `tests/fixtures/`, niet de IssueViews."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["issues"]


class FakeClient:
    """De volledige lees- en schrijfkant van LinearClient, in het geheugen."""

    def __init__(
        self,
        issues: Sequence[IssueView] | None = None,
        *,
        dispatcher_user_id: str = "user-spil",
        dry_run: bool = False,
        issue_count: int = 42,
    ) -> None:
        self.raw_issues = load_fixture()
        seeded = list(issues) if issues is not None else [issue_from_raw(raw) for raw in self.raw_issues]
        self.issues: dict[str, IssueView] = {issue.id: issue for issue in seeded}
        self.comments_by_issue: dict[str, list[CommentView]] = {}
        self.sessions: dict[str, list[AgentSessionView]] = {}
        self.mutations: list[MutationRecord] = []
        self.attachments: list[tuple[str, str, str]] = []
        self.dispatcher_user_id = dispatcher_user_id
        self.dry_run = dry_run
        self.issue_count = issue_count
        self._failures: dict[str, Exception] = {}
        self._counter = 0

    # ---------- testgereedschap ----------

    def fail_next(self, mutation_name: str, exc: Exception) -> None:
        """Laat de eerstvolgende aanroep van deze mutatie deze fout gooien."""
        self._failures[mutation_name] = exc

    def add_comment(self, issue_id: str, body: str, *, author_id: str, author_name: str = "Mens",
                    author_is_app: bool = False, created_at: Optional[datetime] = None) -> CommentView:
        """Een comment van iemand anders dan de dispatcher, voor poorttests."""
        comment = CommentView(
            id=self._next_id("comment"),
            body=body,
            created_at=created_at or self._now(),
            author_id=author_id,
            author_name=author_name,
            author_is_app=author_is_app,
        )
        self.comments_by_issue.setdefault(issue_id, []).append(comment)
        return comment

    def all_issues(self) -> list[IssueView]:
        return list(self.issues.values())

    # ---------- lezen ----------

    def query(self, document: str, variables: dict | None = None) -> dict:
        return {
            "issues": {"nodes": [node_from_issue(i) for i in self.issues.values()]},
            "organization": {"createdIssueCount": self.issue_count},
            "viewer": {"id": self.dispatcher_user_id, "name": "Spil"},
        }

    def paginate(self, document: str, path: str, variables: dict | None = None, page_size: int = 50) -> list[dict]:
        node: Any = self.query(document, variables)
        for part in path.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        return node.get("nodes", []) if isinstance(node, dict) else []

    def issue(self, identifier_or_id: str) -> IssueView:
        for issue in self.issues.values():
            if identifier_or_id in (issue.id, issue.identifier):
                return issue
        raise KeyError(f"onbekend issue {identifier_or_id}")

    def comments(self, issue_id: str, *, limit: int = 50) -> list[CommentView]:
        return self.comments_by_issue.get(issue_id, [])[-limit:]

    def agent_sessions(self, issue_id: str) -> list[AgentSessionView]:
        return self.sessions.get(issue_id, [])

    def label_ids(self) -> Mapping[str, str]:
        found: dict[str, str] = {}
        for issue in self.issues.values():
            found.update(issue.label_ids)
        return found

    def state_ids(self, team_key: str) -> Mapping[str, str]:
        return {
            issue.state_name: issue.state_id for issue in self.issues.values() if issue.team_key == team_key
        }

    def organization_issue_count(self) -> int:
        return self.issue_count

    # ---------- schrijven ----------

    def create_comment(self, issue_id: str, body: str, *, run_id: str) -> Optional[str]:
        gate.assert_not_gate_opening(body, author_is_agent=True)
        self._maybe_fail("commentCreate")
        comment_id = self._next_id("comment")
        self.comments_by_issue.setdefault(issue_id, []).append(
            CommentView(
                id=comment_id,
                body=body,
                created_at=self._now(),
                author_id=self.dispatcher_user_id,
                author_name="Spil",
                author_is_app=False,
            )
        )
        self._record("commentCreate", issue_id, {"body_len": len(body)}, comment_id, run_id)
        return None if self.dry_run else comment_id

    def update_issue(
        self,
        issue_id: str,
        *,
        run_id: str,
        state: str | None = None,
        added_labels: Sequence[str] = (),
        removed_labels: Sequence[str] = (),
        assignee_id: str | None = None,
        delegate_id: str | None = None,
        clear_delegate: bool = False,
        priority: int | None = None,
        description: str | None = None,
        gate_ok: bool = False,
        clear_assignee: bool = False,
        current_state: str | None = None,
        team_key: str | None = None,
    ) -> None:
        issue = self.issues[issue_id]
        for label in added_labels:
            if label in GATE_LABELS:
                raise WriteRefused(f"een agent mag {label} niet zetten")
        if UNREMOVABLE in removed_labels:
            raise WriteRefused("de noodstop mag alleen door een mens uit")
        if issue.state_name.startswith("Poort") and state and state != issue.state_name and not gate_ok:
            raise WriteRefused("een poortstatus verlaten mag alleen na een geldige poortwaarneming")
        self._maybe_fail("issueUpdate")

        labels = [label for label in issue.labels if label not in removed_labels]
        labels += [label for label in added_labels if label not in labels]
        refuse_exclusive_conflicts(labels)
        changes: dict[str, Any] = {"labels": tuple(sorted(labels))}
        if state:
            changes["state_name"] = state
            changes["state_id"] = f"state-{state.lower().replace(' ', '-')}"
        if clear_assignee:
            changes["assignee_id"] = None
        elif assignee_id is not None:
            changes["assignee_id"] = assignee_id
        if clear_delegate:
            changes["delegate_id"] = None
        elif delegate_id is not None:
            changes["delegate_id"] = delegate_id
        if priority is not None:
            changes["priority"] = priority
        if description is not None:
            changes["description"] = description
        if not self.dry_run:
            self.issues[issue_id] = _replace(issue, changes)
        self._record(
            "issueUpdate",
            issue_id,
            {
                "stateId": state,
                "addedLabelIds": list(added_labels),
                "removedLabelIds": list(removed_labels),
                "assigneeId": None if clear_assignee else assignee_id,
                "delegateId": None if clear_delegate else delegate_id,
                "priority": priority,
            },
            issue_id,
            run_id,
        )

    def attach_link(self, issue_id: str, url: str, title: str, *, run_id: str) -> Optional[str]:
        self._maybe_fail("attachmentLinkURL")
        self.attachments.append((issue_id, url, title))
        attachment_id = self._next_id("attachment")
        self._record("attachmentLinkURL", issue_id, {"url": url, "title": title}, attachment_id, run_id)
        return None if self.dry_run else attachment_id

    # ---------- intern ----------

    def _record(self, mutation: str, entity_id: str, variables: dict, result_id: str, run_id: str) -> None:
        summary = {key: value for key, value in variables.items() if key in SUMMARY_KEYS and value not in (None, [])}
        digest = hashlib.sha256(json.dumps(variables, sort_keys=True, default=str).encode()).hexdigest()
        self.mutations.append(
            MutationRecord(
                at=self._now(),
                run_id=run_id,
                mutation=mutation,
                entity_id=entity_id,
                variables_digest=digest,
                variables_summary=summary,
                result_id=None if self.dry_run else result_id,
                ok=True,
                error=None,
                dry_run=self.dry_run,
            )
        )

    def _maybe_fail(self, mutation: str) -> None:
        exc = self._failures.pop(mutation, None)
        if exc is not None:
            raise exc

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)


def _replace(issue: IssueView, changes: Mapping[str, Any]) -> IssueView:
    return replace(issue, **dict(changes))


def make_issue(**overrides) -> IssueView:
    """Een issue in de vorm van WV-207, met alles overschrijfbaar."""
    issue = issue_from_raw(load_fixture()[0])
    if not overrides:
        return issue
    return _replace(issue, overrides)


def make_run(**overrides) -> RunRecord:
    """Een ledgerregel met plausibele waarden."""
    now = datetime.now(timezone.utc)
    defaults = dict(
        run_id="3f9a2c",
        issue_id="490eb350-0000-0000-0000-00004897ad34",
        issue_identifier="WV-207",
        team_key="WV",
        rol="redacteur",
        model="claude-sonnet-5",
        executor="claude",
        klant="raderwerk",
        dienst="content",
        gestart=now,
        geeindigd=now,
        duur_s=761.0,
        beurten=38,
        tokens_in=184203,
        tokens_uit=12044,
        cache_lees=902110,
        kosten_usd=4.21,
        kosten_eur=3.63,
        dod="6/6",
        uitkomst="klaar",
        volgende_status="Agentreview",
        pr_url="https://github.com/raderwerk/raderwerk-content/pull/7",
        artefacten=(Artifact("pr", "https://github.com/raderwerk/raderwerk-content/pull/7", "PR #7"),),
        metered=True,
    )
    defaults.update(overrides)
    return RunRecord(**defaults)


def make_session(**overrides) -> AgentSessionView:
    """Een Agent Session van Codex, standaard op `complete`."""
    now = datetime.now(timezone.utc)
    defaults = dict(
        id="session-1",
        status="complete",
        summary="Klaar. PR: https://github.com/raderwerk/raderwerk-content/pull/7",
        app_user_id="user-codex",
        app_user_name="Codex",
        created_at=now,
        updated_at=now,
        activities=(ActivityView("act-1", "response", "PR geopend", now),),
        pull_request_url="https://github.com/raderwerk/raderwerk-content/pull/7",
    )
    defaults.update(overrides)
    return AgentSessionView(**defaults)


def temp_store(tmpdir) -> Store:
    """Een echte sqlite-Store op een tijdelijk bestand."""
    return Store(Path(tmpdir) / "spil.sqlite3")


class FakeExecutor:
    """SyncExecutor die altijd hetzelfde teruggeeft, of altijd stukgaat."""

    name = "claude"

    def __init__(self, result) -> None:
        self.result = result
        self.requests: list[Any] = []

    def run(self, req):
        self.requests.append(req)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result
