"""De noodrem en de budgetwacht.

Eén label, drie schaalniveaus (spec 8.5). De Spil mag de noodstop **aanzetten**
maar nooit **uitzetten**; dat laatste is slot 5 in `client.update_issue` en is
daarmee geen belofte in een prompt maar een ontbrekende mogelijkheid.

De budgetwacht leest elke ronde `organization.createdIssueCount` af tegen drie
drempels (spec 10): waarschuwen, alleen nog incidenten, noodstop.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

from . import comments
from .client import LinearClient
from .models import IssueView, SwitchState
from .store import Store

__all__ = ["read_switches", "halt_everything", "trip_emergency_stop",
           "GLOBAL_PAUSE_LABEL", "ISSUE_PAUSE_LABEL", "ENGINE_DEAD_LABEL",
           "PANEL_UNREACHABLE"]

GLOBAL_PAUSE_LABEL = "schakelaar/pauze-alles"
ISSUE_PAUSE_LABEL = "schakelaar/pauze"
ENGINE_DEAD_LABEL = "schakelaar/motor-dood"
BUSY_LABEL = "run/bezet"
QUEUE_LABEL = "run/wachtrij"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


PANEL_UNREACHABLE = "bedieningspaneel onbereikbaar"


def read_switches(client: LinearClient, panel: Optional[IssueView],
                  issues: Sequence[IssueView], *, issue_count: int,
                  thresholds: tuple[int, int, int],
                  panel_required: bool = False) -> SwitchState:
    """Leest de drie schakelaars en het issuebudget uit één pollronde.

    `client` wordt hier bewust niet bevraagd: alles staat al in de gebatchte
    leesronde. De parameter blijft in de handtekening omdat de aanroeper hem
    heeft en een latere uitbreiding hem nodig heeft.

    Is er een paneel geconfigureerd maar niet gevonden, dan valt deze functie
    dicht en niet open: `schakelaar/pauze-alles` staat op dat ene issue, dus een
    paneel dat we niet kunnen lezen is een noodstop die we niet kunnen zien.
    """
    warn, restrict, stop = thresholds
    reasons: list[str] = []

    global_pause = bool(panel and GLOBAL_PAUSE_LABEL in panel.labels)
    if global_pause:
        reasons.append(f"{GLOBAL_PAUSE_LABEL} staat op {panel.identifier if panel else '?'}")
    elif panel is None and panel_required:
        global_pause = True
        reasons.append(f"{PANEL_UNREACHABLE}: de noodstop is niet te lezen, dus ik claim niets")

    if issue_count >= stop:
        budget_level = "stop"
        reasons.append(
            f"issueteller {issue_count} >= {stop}: noodstop en een besluit van een mens")
    elif issue_count >= restrict:
        budget_level = "restrict"
        reasons.append(f"issueteller {issue_count} >= {restrict}: alleen nog soort/incident")
    elif issue_count >= warn:
        budget_level = "warn"
        reasons.append(
            f"issueteller {issue_count} >= {warn}: waarschuwing op het bedieningspaneel")
    else:
        budget_level = "ok"

    engine_dead = bool(panel and ENGINE_DEAD_LABEL in panel.labels)
    if engine_dead:
        reasons.append(f"{ENGINE_DEAD_LABEL} staat op het bedieningspaneel")

    return SwitchState(
        global_pause=global_pause,
        paused_issue_ids=frozenset(i.id for i in issues if ISSUE_PAUSE_LABEL in i.labels),
        engine_dead=engine_dead,
        issue_count=issue_count,
        budget_level=budget_level,
        reason="; ".join(reasons) or None,
    )


def halt_everything(client: LinearClient, store: Store, switches: SwitchState, *,
                    run_id: str) -> int:
    """Zet elke openstaande claim terug op `run/wachtrij`. Geeft het aantal terug.

    Eén afbreekcomment per geraakt issue. Het comment op het bedieningspaneel
    schrijft de aanroeper met `comments.halt_comment`, omdat die het paneel en de
    verstreken tijd sinds de omschakeling heeft en deze functie niet.
    """
    if not switches.global_pause:
        return 0
    now = _utcnow()
    aborted = 0
    for claim in store.open_claims():
        issue_id = claim["issue_id"]
        client.create_comment(issue_id, "\n\n".join([
            comments.signature("Spil (dispatcher)", "geen model", run_id, now),
            f"Noodstop actief ({switches.reason or GLOBAL_PAUSE_LABEL}). Ik heb run "
            f"{claim['run_id']} afgebroken en dit issue teruggezet op `run/wachtrij`.",
        ]), run_id=run_id)
        client.update_issue(issue_id, run_id=run_id, added_labels=[QUEUE_LABEL],
                            removed_labels=[BUSY_LABEL])
        store.release_claim(issue_id, claim["run_id"], "afgebroken", now)
        aborted += 1
    return aborted


def trip_emergency_stop(client: LinearClient, panel: IssueView, reason: str, *,
                        run_id: str) -> None:
    """Zet de noodstop aan op het bedieningspaneel, met één comment erbij.

    Weghalen kan de Spil niet: `update_issue` weigert `schakelaar/pauze-alles`
    in `removed_labels`.
    """
    now = _utcnow()
    if GLOBAL_PAUSE_LABEL not in panel.labels:
        client.update_issue(panel.id, run_id=run_id, added_labels=[GLOBAL_PAUSE_LABEL])
    client.create_comment(panel.id, "\n\n".join([
        comments.signature("Spil (dispatcher)", "geen model", run_id, now),
        f"Ik heb `{GLOBAL_PAUSE_LABEL}` zelf aangezet. Reden: {reason}",
        "De hele werkplaats staat stil tot een mens dit label weghaalt. Ik kan dat niet; die "
        "mogelijkheid bestaat niet in mijn code.",
    ]), run_id=run_id)
