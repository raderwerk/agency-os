"""Gedeelde datatypes van de Spil. Pure data, geen I/O.

Alles wat module B en C van Linear te zien krijgen komt in deze vorm binnen.
Elke dataclass is bevroren: een IssueView is een momentopname van één poll en
mag daarna niet meer veranderen, anders is het handelingenlogboek niet meer te
vertrouwen.

Twee regels die overal gelden (docs/architecture.md 3.1):

* labelnamen zijn **canoniek**: `f"{parent.name}/{name}"` als het label in een
  groep zit, anders de kale naam. `repo/raderwerk/raderwerk-content` heeft er
  dus twee schuine strepen in.
* tijdstempels zijn tijdzonebewuste UTC-datetimes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional

__all__ = [
    "Contract",
    "IssueView",
    "CommentView",
    "ActivityView",
    "AgentSessionView",
    "Artifact",
    "RunRecord",
    "SwitchState",
    "Claim",
    "GateObservation",
    "MutationRecord",
    "PollResult",
    "canonical_label_name",
]

_CONTRACT_HEADING = re.compile(r"^#{1,6}\s*Opdrachtcontract\s*$", re.IGNORECASE | re.MULTILINE)
_FENCE = re.compile(r"^\s*(```|~~~)\s*([A-Za-z0-9_-]*)\s*$")

# Sleutels die het contract zelf kent. Al het andere komt in `unknown_keys`
# terecht en gaat ongewijzigd mee de prompt in -- weggooien is liegen.
_KNOWN_CONTRACT_KEYS = frozenset(
    {"contract", "versie", "version", "klant", "repo", "basisbranch", "omgeving",
     "publiek", "bronnen", "verboden"}
)
_TRUE = frozenset({"true", "ja", "yes", "waar", "1"})
_FALSE = frozenset({"false", "nee", "no", "onwaar", "0"})


def canonical_label_name(name: str, parent_name: Optional[str]) -> str:
    """De canonieke labelnaam: groep + leaf, of de kale leaf.

    Linear geeft in `Issue.labels.nodes[].name` alleen het blad terug
    (`"contentstuk"`), met de groep in `parent.name` (`"soort"`). Elke
    vergelijking in deze codebase gebruikt de canonieke naam.
    """
    if parent_name:
        return f"{parent_name}/{name}"
    return name


def _yaml_scalar(raw: str) -> Optional[str]:
    value = raw.strip()
    if value.startswith("#"):
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value or None


@dataclass(frozen=True)
class Contract:
    """Het eerste yaml-blok onder '## Opdrachtcontract'.

    Onbekende sleutels worden bewaard, niet weggegooid: een sjabloon dat
    `eindredacteur:` of `budget_kader_eur:` toevoegt mag niet stil verdwijnen.
    """

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
    def parse(description: Optional[str]) -> Optional["Contract"]:
        """Leest het contractblok uit een issue-omschrijving. None als het ontbreekt."""
        if not description:
            return None
        heading = _CONTRACT_HEADING.search(description)
        if heading is None:
            return None
        block = _first_fenced_block(description[heading.end():])
        if block is None:
            return None
        pairs, lists = _parse_mini_yaml(block)
        if not pairs and not lists:
            return None
        version = pairs.get("contract") or pairs.get("versie") or pairs.get("version") or "v1"
        seen = list(pairs) + list(lists)
        unknown = tuple(sorted(k for k in seen if k not in _KNOWN_CONTRACT_KEYS))
        publiek_raw = (pairs.get("publiek") or "").lower()
        return Contract(
            version=version,
            klant=pairs.get("klant"),
            repo=pairs.get("repo"),
            basisbranch=pairs.get("basisbranch") or "main",
            omgeving=pairs.get("omgeving") or "geen",
            publiek=publiek_raw in _TRUE,
            bronnen=tuple(lists.get("bronnen", ())),
            verboden=tuple(lists.get("verboden", ())),
            unknown_keys=unknown,
            raw=block,
        )


def _first_fenced_block(text: str) -> Optional[str]:
    """De inhoud van het eerste fenced blok in `text`, zonder de fences zelf."""
    fence: Optional[str] = None
    body: list[str] = []
    for line in text.splitlines():
        match = _FENCE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)
            elif line.strip() and not line.strip().startswith("#"):
                # Er staat gewone tekst vóór het blok; dat is geen contract.
                continue
            continue
        if match and match.group(1) == fence:
            return "\n".join(body)
        body.append(line)
    return None


def _parse_mini_yaml(block: str) -> tuple[dict[str, Optional[str]], dict[str, list[str]]]:
    """Genoeg yaml voor het contractblok: `sleutel: waarde` en `  - item`.

    Bewust geen volledige yaml-lezer. Het contractblok is een vast, plat
    sjabloon (spec 5.11); een echte parser zou hier alleen maar meer kunnen
    accepteren dan het sjabloon toestaat.
    """
    pairs: dict[str, Optional[str]] = {}
    lists: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in block.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            if current is not None:
                item = _yaml_scalar(stripped[2:])
                if item:
                    lists.setdefault(current, []).append(item)
            continue
        if ":" not in stripped:
            continue
        key, _, rest = stripped.partition(":")
        key = key.strip()
        value = _yaml_scalar(rest)
        if value is None:
            current = key
            lists.setdefault(key, [])
        else:
            current = None
            pairs[key] = value
    return pairs, {k: v for k, v in lists.items() if v or k not in pairs}


@dataclass(frozen=True)
class IssueView:
    """Eén issue zoals de Spil het deze cyclus heeft gezien."""

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
        """Het blad van het label in deze groep, of None.

        `label_in_group("repo")` op `repo/raderwerk/raderwerk-content` geeft
        `"raderwerk/raderwerk-content"`: er wordt op de eerste schuine streep
        gesplitst en verder nergens.
        """
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
        """Het klantlabel; bij afwezigheid de klant uit het contract."""
        from_label = self.label_in_group("klant")
        if from_label:
            return from_label
        if self.contract and self.contract.klant and self.contract.klant != "geen":
            return self.contract.klant
        return None

    @property
    def risico(self) -> str:
        """`laag` als het risicolabel ontbreekt -- niet None, want dit stuurt gedrag."""
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
        """Ongegroepeerde vlaggen: `bewijs-ontbreekt`, `lus-verdacht`, `geënsceneerd`, ..."""
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
    """Eén regel in het kostenboek. Ook mislukte runs krijgen er een."""

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
    """Wat de Spil bij een poort heeft gezien, inclusief waarom hij het weigert."""

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
