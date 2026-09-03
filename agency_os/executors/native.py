"""De native lanen: Codex en Cursor aanstoten en hun Agent Session bewaken.

Zie docs/architecture.md sectie 10.2 en agent-roster.md sectie 4. De comment is
de trigger: `delegateId` opnieuw zetten start geen sessie (geverifieerd
2026-09-03). Twee opeenvolgende polls op `awaitingInput`, `error` of `stale`
leveren de terugvalprocedure op — nooit stilzwijgend doorgaan met één reviewer.

Deze module schrijft niet zelf naar Linear: elke schrijfactie loopt via de
client die C meegeeft, en dus via de schrijfwachters van module A.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Optional, Sequence

from agency_os.executors.base import (
    Artifact,
    ExecutionRequest,
    ExecutionResult,
    ExecutorConfig,
    ExecutorError,
    TriggerReceipt,
    Usage,
    utcnow,
)

if TYPE_CHECKING:  # pragma: no cover - alleen voor typecontrole
    from agency_os.linear.models import AgentSessionView, IssueView

__all__ = [
    "FALLBACK_ERROR",
    "NativeExecutor",
    "PR_URL_RE",
    "STRIKE_LIMIT",
    "extract_pr_url",
    "fallback_comment",
    "mention_body",
]

#: Machineleesbaar begin van `ExecutionResult.error`, zodat C naar de Claude-tegenhanger routeert.
FALLBACK_ERROR = "native-vastgelopen"

#: Twee opeenvolgende polls op een vastgelopen status, dan valt de Spil terug.
STRIKE_LIMIT = 2

PR_URL_RE = re.compile(r"https://github\.com/raderwerk/[\w.-]+/pull/\d+")

_APP_NAMES = {"codex": "Codex", "cursor": "Cursor"}
_RUNNING = frozenset({"pending", "active"})
_STUCK = frozenset({"awaitingInput", "error", "stale"})
_AMSTERDAM = "Europe/Amsterdam"


def _local(when: datetime) -> str:
    """Tijdstempel zoals een mens hem in Linear leest: '2026-09-03 11:14'."""
    try:
        from zoneinfo import ZoneInfo

        return when.astimezone(ZoneInfo(_AMSTERDAM)).strftime("%Y-%m-%d %H:%M")
    except (ImportError, KeyError, OSError):  # pragma: no cover - zonder tzdata blijft UTC over
        return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def mention_body(agent: str, req: ExecutionRequest) -> str:
    """De comment die de sessie start, in de geverifieerde vorm per agent.

    Codex heeft `in raderwerk/<repo>` nodig (zonder cloudomgeving per repo faalt
    de delegatie met "failed to start"), Cursor wil `repo=` en `branch=`.
    """
    if not req.repo:
        raise ExecutorError(f"{_APP_NAMES[agent]} kan niets doen zonder repo op het issue")
    app = _APP_NAMES[agent]
    issue = req.issue
    opdracht = f"pak {issue.identifier} op ({req.role_title}): {issue.title}"
    if agent == "codex":
        first = f"@{app} {opdracht} in {req.repo}"
    else:
        first = f"@{app} {opdracht} repo={req.repo} branch={req.base_branch}"
    return "\n".join(
        [
            first,
            "",
            f"Issue: {issue.url}",
            f"Branch: {req.branch} (vertak vanaf {req.base_branch})",
            "Open een pull request en merge niet; de merge is een poort en dus mensenwerk.",
        ]
    )


def fallback_comment(app_name: str, status: str, since: datetime, run_id: str,
                     when: Optional[datetime] = None) -> str:
    """De letterlijke terugvaltekst uit agent-roster.md sectie 4."""
    return (
        f"De tweede reviewer ({app_name}) was niet beschikbaar: sessie stond op {status} "
        f"sinds {_local(since)}. Ik val terug op de Claude-tegenhanger. "
        "Dit is geen volwaardige dubbele review.\n\n"
        f"**Spil · {app_name}-lane · run {run_id} · {_local(when or utcnow())}**"
    )


def extract_pr_url(session: "AgentSessionView") -> Optional[str]:
    """De PR-link uit de sessie: eerst het veld, dan de samenvatting, dan de activiteiten."""
    if getattr(session, "pull_request_url", None):
        return session.pull_request_url
    haystacks = [session.summary or ""]
    haystacks += [activity.body or "" for activity in session.activities]
    for text in haystacks:
        match = PR_URL_RE.search(text)
        if match:
            return match.group(0)
    return None


class NativeExecutor:
    """Stoot Codex of Cursor aan en bewaakt de sessie. Implementeert `AsyncExecutor`."""

    def __init__(self, cfg: ExecutorConfig, agent: str, *,
                 now: Callable[[], datetime] = utcnow) -> None:
        if agent not in _APP_NAMES:
            raise ValueError(f"onbekende native agent: {agent!r}")
        self.cfg = cfg
        self.agent = agent
        self.app_name = _APP_NAMES[agent]
        self.name = f"native-{agent}"
        # De klok is een parameter en geen import: `_expired` vergelijkt tegen
        # `receipt.triggered_at`, en een test met een vaste triggertijd mag niet
        # afhangen van hoe laat het toevallig is (bevinding 1/16).
        self.now = now

    # -- aanstoten ---------------------------------------------------------

    def trigger(self, client, req: ExecutionRequest) -> TriggerReceipt:
        """Plaats de mention-comment. Dat is wat de sessie werkelijk start."""
        comment_id = None
        if not (req.dry_run or self.cfg.dry_run):
            comment_id = client.create_comment(
                req.issue.id, mention_body(self.agent, req), run_id=req.run_id
            )
        return TriggerReceipt(
            run_id=req.run_id,
            issue_id=req.issue.id,
            executor=self.name,
            trigger_comment_id=comment_id,
            session_id=None,
            triggered_at=self.now(),
            strikes=0,
        )

    # -- bewaken -----------------------------------------------------------

    def poll(
        self, client, receipt: TriggerReceipt, issue: "IssueView"
    ) -> tuple[TriggerReceipt, Optional[ExecutionResult]]:
        """Eén ronde kijken hoe de sessie ervoor staat (sectie 10.2)."""
        session = self._own_session(client.agent_sessions(issue.id), receipt)
        if session is None:
            if self._expired(receipt):
                return receipt, self._fall_back(client, issue, receipt, "geen sessie", self.now())
            return receipt, None

        receipt = replace(receipt, session_id=session.id)

        if session.status == "complete":
            return receipt, self._completed(receipt, session)

        if session.status in _RUNNING:
            self._claim_delegate(client, issue, session, receipt)
            receipt = replace(receipt, strikes=0)
            if self._expired(receipt):
                return receipt, self._fall_back(
                    client, issue, receipt, session.status, session.updated_at
                )
            return receipt, None

        if session.status not in _STUCK:  # pragma: no cover - Linear kent geen andere statussen
            return receipt, None

        receipt = replace(receipt, strikes=receipt.strikes + 1)
        if receipt.strikes < STRIKE_LIMIT:
            return receipt, None
        return receipt, self._fall_back(client, issue, receipt, session.status, session.updated_at)

    # -- onderdelen --------------------------------------------------------

    def _own_session(
        self, sessions: Sequence["AgentSessionView"], receipt: TriggerReceipt
    ) -> Optional["AgentSessionView"]:
        """De sessie die bij deze run hoort: op id, anders de nieuwste van deze app."""
        if receipt.session_id:
            for session in sessions:
                if session.id == receipt.session_id:
                    return session
        mine = [
            session
            for session in sessions
            if session.app_user_name == self.app_name
            and session.created_at >= receipt.triggered_at
        ]
        return max(mine, key=lambda session: session.created_at, default=None)

    def _expired(self, receipt: TriggerReceipt) -> bool:
        age = (self.now() - receipt.triggered_at).total_seconds()
        return age > self.cfg.native_session_timeout_s

    def _claim_delegate(self, client, issue: "IssueView", session, receipt: TriggerReceipt) -> None:
        """Zet `delegateId` zodra de app-user bekend is; het bord hoort te kloppen."""
        if issue.delegate_id or not session.app_user_id or self.cfg.dry_run:
            return
        client.update_issue(issue.id, run_id=receipt.run_id, delegate_id=session.app_user_id)

    def _completed(self, receipt: TriggerReceipt, session) -> ExecutionResult:
        pr_url = extract_pr_url(session)
        summary = session.summary or f"{self.app_name} rondde de sessie af."
        if not pr_url:
            summary += " Er staat geen PR-link in de sessie."
        return ExecutionResult(
            run_id=receipt.run_id,
            uitkomst="klaar",
            summary_md=summary,
            dod="-",
            question=None,
            error=None,
            pr_url=pr_url,
            branch=None,
            artifacts=(Artifact("pr", pr_url, f"PR via {self.app_name}"),) if pr_url else (),
            usage=Usage(
                duration_s=(session.updated_at - session.created_at).total_seconds(),
                source="native-unmetered",
                metered=False,
            ),
            started_at=receipt.triggered_at,
            ended_at=self.now(),
            session_id=session.id,
            raw_log_path=None,
        )

    def _fall_back(
        self, client, issue: "IssueView", receipt: TriggerReceipt, status: str, since: datetime
    ) -> ExecutionResult:
        """De vier handelingen uit roster sectie 4, in volgorde."""
        labels = ["run/vastgelopen"]
        if self._is_second_reviewer(issue):
            labels.append("bewijs-ontbreekt")
        body = fallback_comment(self.app_name, status, since, receipt.run_id, self.now())

        if not self.cfg.dry_run:
            client.update_issue(
                issue.id, run_id=receipt.run_id, clear_delegate=True, added_labels=labels
            )
            client.create_comment(issue.id, body, run_id=receipt.run_id)

        return ExecutionResult(
            run_id=receipt.run_id,
            uitkomst="mislukt",
            summary_md=body,
            dod="-",
            question=None,
            error=f"{FALLBACK_ERROR}: {self.app_name} stond op {status}; terug naar de router",
            pr_url=None,
            branch=None,
            artifacts=(),
            usage=Usage(source="native-unmetered", metered=False),
            started_at=receipt.triggered_at,
            ended_at=self.now(),
            session_id=receipt.session_id,
            raw_log_path=None,
        )

    @staticmethod
    def _is_second_reviewer(issue: "IssueView") -> bool:
        """Een vastgelopen sessie in Agentreview op `risico/hoog` kost het tweede oordeel."""
        return issue.state_name == "Agentreview" and issue.high_risk
