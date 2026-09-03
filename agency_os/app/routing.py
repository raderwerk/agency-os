"""Routering: van (bord, status, labels) naar een rol, een model en een uitvoerder.

De routeringstabel is data, geen code (spec hoofdstuk 3 en agent-roster.md sectie
3): `agency_os/roles/routing.json` bevat de rollen en de regels, deze module
leest ze en past de overrides toe. Er staat hier bewust geen enkele `if` die een
klant of een dienst bij naam noemt -- een fout in de tabel corrigeert zichzelf
niet, dus elke beslissing draagt een leesbare reden mee die in de comment belandt.

Deze module importeert niets uit `agency_os.linear` of `agency_os.executors`:
het issue wordt via zijn eigen afgeleide eigenschappen bevraagd
(`label_in_group`, `estimate`, `repo`), zodat de tabel testbaar is zonder client.

Van de Store gebruikt `loop_guard` precies één methode:
`role_run_count(issue_id, role, day) -> int` (tabel `role_runs` uit spec 5.1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional

ROLES_DIR = Path(__file__).resolve().parent.parent / "roles"
ROUTING_TABLE_PATH = ROLES_DIR / "routing.json"


class RoutingError(RuntimeError):
    """De routeringstabel zelf deugt niet. Fataal bij het opstarten."""


@dataclass(frozen=True)
class ModelSpec:
    """Eén model uit de modelverdeling van agent-roster.md."""

    key: str
    display: str
    ledger: str
    family: str
    executor: str


MODELS: Mapping[str, ModelSpec] = {
    "fable": ModelSpec("fable", "Claude Fable 5.1", "claude-fable-5-1", "claude", "claude"),
    "opus": ModelSpec("opus", "Claude Opus 5", "claude-opus-5", "claude", "claude"),
    "sonnet": ModelSpec("sonnet", "Claude Sonnet 5", "claude-sonnet-5", "claude", "claude"),
    "codex": ModelSpec("codex", "Codex GPT-5.6 Sol xhigh", "gpt-5.6-sol", "openai", "native-codex"),
    # Hetzelfde model als `codex`, maar via de lokale CLI in plaats van een
    # mention in Linear. De reviewer heeft die variant nodig: hij moet binnen
    # dezelfde cyclus een oordeel geven, en een cloudsessie levert dat pas cycli
    # later. Staat daarom bewust niet in NATIVE_MODELS.
    "codex-cli": ModelSpec("codex-cli", "Codex GPT-5.6 Sol xhigh (CLI)", "gpt-5.6-sol",
                           "openai", "codex-cli"),
    "cursor": ModelSpec("cursor", "Cursor Grok 4.6", "cursor-grok-4.6-high-fast", "xai", "native-cursor"),
}

NATIVE_MODELS = frozenset({"codex", "cursor"})
XL_ESTIMATE = 5


@dataclass(frozen=True)
class RoleSpec:
    """Eén rol uit `routing.json`."""

    key: str
    title: str
    prompt_file: str
    default_model: str
    family: str
    executor: str
    working_state: str
    done_state: str
    needs_worktree: bool
    needs_pr: bool
    #: Mag `agent/<model>` op het issue het model van deze rol overrulen? Waar
    #: voor de rollen die het werk máken, onwaar voor de rollen die het
    #: beoordelen. Zie `decide` voor waarom dat verschil moet bestaan.
    model_from_label: bool = True

    @property
    def prompt_path(self) -> Path:
        return ROLES_DIR / self.prompt_file


@dataclass(frozen=True)
class Rule:
    """Eén regel: bord + status + labelvoorwaarden -> rol."""

    team: str
    state: str
    when: Mapping[str, tuple[str, ...]]
    role: str

    def matches(self, issue: Any) -> bool:
        """Waar als bord en status kloppen en elke `when`-sleutel is voldaan."""
        if issue.team_key != self.team or issue.state_name != self.state:
            return False
        return all(issue.label_in_group(group) in values for group, values in self.when.items())


@dataclass(frozen=True)
class RoutingTable:
    """De ingelezen tabel, inclusief het pad waar hij vandaan komt."""

    version: int
    roles: Mapping[str, RoleSpec]
    rules: tuple[Rule, ...]
    path: Path

    def match(self, issue: Any) -> Optional[Rule]:
        """De eerste regel die past, of None."""
        for rule in self.rules:
            if rule.matches(issue):
                return rule
        return None


@dataclass(frozen=True)
class Route:
    """Wat er met dit issue gaat gebeuren, met de motivering erbij."""

    role: RoleSpec
    model_key: str
    executor_name: str
    reason: str

    @property
    def model(self) -> ModelSpec:
        return MODELS[self.model_key]


@dataclass(frozen=True)
class Refusal:
    """Waarom dit issue níét geclaimd wordt, en wat er in plaats daarvan moet."""

    code: str  # geen-route | agent-mens | xl | geen-repo
    reason: str
    action: str  # overslaan | backlog | vraag


def load_table(path: Path | str = ROUTING_TABLE_PATH) -> RoutingTable:
    """Leest en valideert `routing.json`.

    Valideert wat bij het opstarten fataal moet zijn (spec hoofdstuk 4): elke
    regel wijst naar een bestaande rol, elk model bestaat, en elk `prompt_file`
    staat er echt.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RoutingError(f"routeringstabel ontbreekt: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RoutingError(f"routeringstabel is geen geldige json: {path}: {exc}") from exc

    roles: dict[str, RoleSpec] = {}
    for key, spec in raw.get("roles", {}).items():
        try:
            roles[key] = RoleSpec(
                key=key,
                title=spec["title"],
                prompt_file=spec["prompt_file"],
                default_model=spec["default_model"],
                family=spec["family"],
                executor=spec["executor"],
                working_state=spec["working_state"],
                done_state=spec["done_state"],
                needs_worktree=bool(spec["needs_worktree"]),
                needs_pr=bool(spec["needs_pr"]),
                model_from_label=bool(spec.get("model_from_label", True)),
            )
        except KeyError as exc:
            raise RoutingError(f"rol {key!r} mist veld {exc.args[0]!r}") from exc
        if roles[key].default_model not in MODELS:
            raise RoutingError(f"rol {key!r} noemt onbekend model {roles[key].default_model!r}")
        if not roles[key].prompt_path.is_file():
            raise RoutingError(f"rol {key!r} verwijst naar ontbrekend promptbestand {spec['prompt_file']!r}")

    rules: list[Rule] = []
    for index, item in enumerate(raw.get("rules", [])):
        role = item.get("role")
        if role not in roles:
            raise RoutingError(f"regel {index} verwijst naar onbekende rol {role!r}")
        when = {group: tuple(values) for group, values in (item.get("when") or {}).items()}
        rules.append(Rule(team=item["team"], state=item["state"], when=when, role=role))

    if not rules:
        raise RoutingError(f"routeringstabel zonder regels: {path}")
    return RoutingTable(version=int(raw.get("version", 1)), roles=roles, rules=tuple(rules), path=path)


def decide(table: RoutingTable, issue: Any, *, allow_fable: bool) -> Route | Refusal:
    """De volledige beslissing: een Route, of een Refusal met een vervolgactie.

    De volgorde is die van spec hoofdstuk 9: eerst de regel, dan `agent/mens`,
    dan de native lane, dan de modeloverride, dan XL, dan de repo-eis.
    """
    rule = table.match(issue)
    if rule is None:
        return Refusal("geen-route", f"geen regel voor {issue.team_key}/{issue.state_name}", "overslaan")
    role = table.roles[rule.role]
    reasons = [f"{issue.team_key}/{issue.state_name} -> {role.title}"]

    if issue.agent_hint == "mens":
        return Refusal("agent-mens", "label agent/mens: dit issue is mensenwerk", "overslaan")

    model_key = role.default_model
    hint = issue.agent_hint
    if hint in MODELS and not role.model_from_label:
        # `agent/<model>` zegt wie het werk máákt. Laat je dat label ook de
        # beoordelende rollen sturen, dan kiest de bouwer zijn eigen reviewer:
        # `agent/fable` op WV-210 zette de reviewer op Claude terwijl de roster
        # juist eist dat hij uit een andere familie komt. Erger nog, de laan
        # blijft `codex-cli`, dus er draait GPT-5.6 terwijl het Kostenboek
        # `claude-opus-5` boekt tegen de prijs van Opus.
        reasons.append(f"label agent/{hint} genegeerd: {role.title} houdt zijn eigen model")
    elif hint in NATIVE_MODELS:
        model_key = hint
        reasons.append(f"native lane via label agent/{hint}")
    elif hint in MODELS:
        model_key = hint
        reasons.append(f"model uit label agent/{hint} (wint van standaard {role.default_model})")
    if model_key == "fable" and not allow_fable:
        model_key = "opus"
        reasons.append("Fable-quotum uit: teruggezet naar Opus 5, dit is een afzwakking en geen keuze")

    if issue.estimate == XL_ESTIMATE:
        return Refusal("xl", "XL is geen uitvoerbaar issue: eerst opknippen (spec 2.3)", "backlog")
    if issue.dienst == "web" and not issue.repo:
        return Refusal(
            "geen-repo",
            "dienst/web zonder repo: geen repo-label en geen repo in het opdrachtcontract",
            "vraag",
        )

    model = MODELS[model_key]
    executor_name = model.executor if model_key in NATIVE_MODELS else role.executor
    reasons.append(f"uitvoerder {executor_name} op {model.display}")
    return Route(role=role, model_key=model_key, executor_name=executor_name, reason="; ".join(reasons))


def resolve(table: RoutingTable, issue: Any, *, allow_fable: bool) -> Optional[Route]:
    """De route voor dit issue, of None als het niet geclaimd mag worden.

    Waarom er geen route is, staat in `decide()`; de planner gebruikt die
    variant omdat een weigering een eigen vervolgactie heeft.
    """
    outcome = decide(table, issue, allow_fable=allow_fable)
    return outcome if isinstance(outcome, Route) else None


def loop_guard(store: Any, issue: Any, role_key: str, day: date) -> Optional[str]:
    """Een reden om te stoppen als deze rol vandaag al op dit issue draaide.

    Strenger dan spec 8.6 (drie per dag): de tweede run van dezelfde rol op
    hetzelfde issue op één dag wordt geweigerd. Zie architectuur sectie 18.1.
    """
    count = int(store.role_run_count(issue.id, role_key, day))
    if count < 1:
        return None
    return (
        f"lusdetectie: rol {role_key} draaide vandaag al {count}x op {issue.identifier}; "
        "een tweede run op dezelfde dag wordt geweigerd"
    )
