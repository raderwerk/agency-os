"""Stand-in voor `agency_os.linear.models` (onderdeel A), contract 3.2.

Zolang A nog niet gemerged is, draait de test hier tegenaan. Zodra
`agency_os/linear/models.py` bestaat, wordt dit bestand niet meer geïnstalleerd
(zie `tests/stubs/__init__.py`) en verandert er niets aan de tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional


@dataclass(frozen=True)
class Contract:
    """Het yaml-blok onder '## Opdrachtcontract'."""

    version: str
    klant: Optional[str]
    repo: Optional[str]
    basisbranch: str
    omgeving: str
    publiek: bool
    bronnen: tuple[str, ...]
    verboden: tuple[str, ...]
    unknown_keys: tuple[str, ...]
    raw: str

    @staticmethod
    def parse(description: str) -> Optional["Contract"]:
        found = re.search(r"##\s*Opdrachtcontract\s*\n+```ya?ml\n(.*?)```", description or "", re.S)
        if not found:
            return None
        raw = found.group(1)
        known = {"versie", "klant", "repo", "basisbranch", "omgeving", "publiek", "bronnen", "verboden"}
        values: dict[str, str] = {}
        unknown: list[str] = []
        for line in raw.splitlines():
            if ":" not in line or line.strip().startswith("-"):
                continue
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            values.setdefault(key, value)
            if key not in known:
                unknown.append(key)
        return Contract(
            version=values.get("versie", "v1"),
            klant=values.get("klant"),
            repo=values.get("repo"),
            basisbranch=values.get("basisbranch", "main"),
            omgeving=values.get("omgeving", "geen"),
            publiek=values.get("publiek", "false").lower() in {"true", "ja"},
            bronnen=(),
            verboden=(),
            unknown_keys=tuple(unknown),
            raw=raw,
        )


@dataclass(frozen=True)
class IssueView:
    """Eén issue zoals de dispatcher het leest."""

    id: str
    identifier: str
    title: str
    description: str
    url: str
    team_key: str
    state_id: str
    state_name: str
    state_type: str
    estimate: Optional[int]
    priority: int
    labels: tuple[str, ...]
    label_ids: Mapping[str, str]
    project_id: Optional[str]
    project_name: Optional[str]
    assignee_id: Optional[str]
    delegate_id: Optional[str]
    updated_at: datetime
    contract: Optional[Contract]

    def label_in_group(self, group: str) -> Optional[str]:
        prefix = f"{group}/"
        for label in self.labels:
            if label.startswith(prefix):
                return label[len(prefix):]
        return None

    @property
    def dienst(self) -> Optional[str]:
        return self.label_in_group("dienst")

    @property
    def soort(self) -> Optional[str]:
        return self.label_in_group("soort")

    @property
    def klant(self) -> Optional[str]:
        return self.label_in_group("klant")

    @property
    def risico(self) -> str:
        return self.label_in_group("risico") or "laag"

    @property
    def agent_hint(self) -> Optional[str]:
        return self.label_in_group("agent")

    @property
    def run_state(self) -> Optional[str]:
        return self.label_in_group("run")

    @property
    def repo(self) -> Optional[str]:
        return self.label_in_group("repo") or (self.contract.repo if self.contract else None)

    @property
    def high_risk(self) -> bool:
        return self.risico == "hoog"

    @property
    def is_gate_state(self) -> bool:
        return self.state_name.startswith("Poort")

    @property
    def flags(self) -> frozenset[str]:
        return frozenset(label for label in self.labels if "/" not in label)


@dataclass(frozen=True)
class CommentView:
    id: str
    body: str
    created_at: datetime
    author_id: str
    author_name: str
    author_is_app: bool


@dataclass(frozen=True)
class ActivityView:
    id: str
    type: str
    body: str
    created_at: datetime


@dataclass(frozen=True)
class AgentSessionView:
    id: str
    status: str
    summary: Optional[str]
    app_user_id: str
    app_user_name: str
    created_at: datetime
    updated_at: datetime
    activities: tuple[ActivityView, ...]
    pull_request_url: Optional[str]


@dataclass(frozen=True)
class Artifact:
    type: str
    url: str
    label: str = ""


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    issue_id: str
    issue_identifier: str
    team_key: str
    rol: str
    model: str
    executor: str
    klant: Optional[str]
    dienst: Optional[str]
    gestart: datetime
    geeindigd: Optional[datetime]
    duur_s: float
    beurten: int
    tokens_in: int
    tokens_uit: int
    cache_lees: int
    kosten_usd: float
    kosten_eur: float
    dod: str
    uitkomst: str
    volgende_status: Optional[str]
    pr_url: Optional[str]
    artefacten: tuple[Artifact, ...]
    metered: bool


@dataclass(frozen=True)
class SwitchState:
    global_pause: bool
    paused_issue_ids: frozenset[str]
    engine_dead: bool
    issue_count: int
    budget_level: str
    reason: Optional[str]


@dataclass(frozen=True)
class Claim:
    run_id: str
    issue_id: str
    issue_identifier: str
    claimed_at: datetime
    comment_id: Optional[str]


@dataclass(frozen=True)
class GateObservation:
    issue_id: str
    gate_state: str
    card_comment_id: Optional[str]
    card_created_at: Optional[datetime]
    outcome: Optional[str]
    token: Optional[str]
    source: Optional[str]
    source_id: Optional[str]
    actor_id: Optional[str]
    actor_name: Optional[str]
    actor_is_app: Optional[bool]
    valid: bool
    refusal: Optional[str]


@dataclass(frozen=True)
class MutationRecord:
    at: datetime
    run_id: Optional[str]
    mutation: str
    entity_id: str
    variables_digest: str
    variables_summary: Mapping[str, object]
    result_id: Optional[str]
    ok: bool
    error: Optional[str]
    dry_run: bool


@dataclass(frozen=True)
class PollResult:
    at: datetime
    panel: Optional[IssueView]
    switches: SwitchState
    ready: tuple[IssueView, ...]
    gates: tuple[IssueView, ...]
    watching: tuple[IssueView, ...]
    skipped: tuple[tuple[str, str], ...]
