"""De ene gebatchte leesronde per cyclus, en de indeling die eruit volgt.

Spec 8.1. Eén GraphQL-document haalt het bedieningspaneel, alle issues van de
twee teams en de issueteller op. Daarna wordt er clientzijdig ingedeeld: wat is
claimbaar, wat zit in een poort, waar loopt al iets, en wat wordt overgeslagen
en waarom. Overgeslagen issues worden gelogd, nooit stilzwijgend genegeerd.

Statusfilters staan bewust niet in de query: `state.type nin [...]` is niet
geverifieerd tegen deze workspace, en een filter dat stil te veel wegfiltert is
erger dan een iets duurdere leesronde op een werkplaats van 250 issues.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from . import queries
from .client import LinearClient
from .killswitch import ISSUE_PAUSE_LABEL, read_switches
from .models import IssueView, PollResult

__all__ = ["PollConfig", "poll", "BLOCKING_LABELS", "DEAD_STATE_TYPES",
           "DEFAULT_ISSUE_BUDGET"]

# Drempels uit spec 10. PollConfig draagt ze niet, dus wie andere drempels wil
# roept `killswitch.read_switches` zelf aan met zijn eigen configuratie.
DEFAULT_ISSUE_BUDGET = (200, 220, 225)

# Labels die een issue uit `ready` houden, met de reden die in het logboek komt.
BLOCKING_LABELS: Mapping[str, str] = {
    ISSUE_PAUSE_LABEL: "gepauzeerd",
    "run/vastgelopen": "vastgelopen",
    "run/onbevestigd": "onbevestigd",
    "schakelaar/mens-vereist": "mens-vereist",
    "agent/mens": "mensenwerk",
}
DEAD_STATE_TYPES = frozenset({"completed", "canceled"})


@dataclass(frozen=True)
class PollConfig:
    team_keys: tuple[str, ...]
    panel_identifier: str
    in_scope_states: Mapping[str, tuple[str, ...]]
    max_claims: int


def poll(client: LinearClient, cfg: PollConfig) -> PollResult:
    """Leest en deelt in. Schrijft niets; dat is de volgende stap van de cyclus."""
    at = datetime.now(timezone.utc)
    nodes = client.paginate(queries.POLL, "issues", {"teamKeys": list(cfg.team_keys)})
    issues = [client.to_issue_view(node) for node in nodes]

    panel = next((i for i in issues if i.identifier == cfg.panel_identifier), None)
    if panel is None and cfg.panel_identifier:
        # Het paneel kan afgesloten zijn en dus buiten de pollronde vallen; dat mag
        # geen crash zijn (architectuur 4: ontbrekend paneel = hartslag overslaan).
        try:
            panel = client.issue(cfg.panel_identifier)
        except Exception:
            panel = None

    issue_count = client.organization_issue_count()
    switches = read_switches(client, panel, issues, issue_count=issue_count,
                             thresholds=DEFAULT_ISSUE_BUDGET)

    ready: list[IssueView] = []
    gates: list[IssueView] = []
    watching: list[IssueView] = []
    skipped: list[tuple[str, str]] = []

    for issue in issues:
        if issue.identifier == cfg.panel_identifier:
            continue
        if issue.state_type in DEAD_STATE_TYPES:
            continue
        if issue.is_gate_state:
            gates.append(issue)
            continue
        if issue.run_state == "bezet":
            watching.append(issue)
            continue
        blocked = next((reason for label, reason in BLOCKING_LABELS.items()
                        if label in issue.labels), None)
        if blocked:
            skipped.append((issue.identifier, blocked))
            continue
        if issue.state_name not in cfg.in_scope_states.get(issue.team_key, ()):
            skipped.append((issue.identifier, "buiten-mvp"))
            continue
        if switches.budget_level == "restrict" and issue.soort != "incident":
            skipped.append((issue.identifier, "issuebudget-restrict"))
            continue
        ready.append(issue)

    ready.sort(key=_claim_order)
    return PollResult(at=at, panel=panel, switches=switches, ready=tuple(ready),
                      gates=tuple(gates), watching=tuple(watching), skipped=tuple(skipped))


def _claim_order(issue: IssueView) -> tuple[int, int, float]:
    """Spec 8.1 stap 5: prioriteit, dan de oudste updatedAt.

    Linear gebruikt 0 voor "geen prioriteit" en 1 voor Urgent, dus 0 hoort
    achteraan en niet vooraan.
    """
    has_priority = 0 if 1 <= issue.priority <= 4 else 1
    return (has_priority, issue.priority, issue.updated_at.timestamp())
