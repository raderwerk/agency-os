"""De poort. Het paranoïdste bestand van de repo, en dat hoort zo.

Een poortstatus is elke status waarvan de naam met `Poort` begint. Bij zo'n
status leest de Spil twee gelijkwaardige kanalen: een comment met een token op
de eerste regel, en een labelwissel naar `poort/akkoord` of `poort/afgekeurd`.

De vijf voorwaarden (D02) worden allemaal gecontroleerd en hebben elk hun eigen
weigertekst. Alles wat niet geldig is leidt naar `mark_unconfirmed`. Er is geen
"toch maar doorgaan met een notitie"-tak in deze code -- niet als beleid, maar
als ontbrekende tak.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

from .. import gate
from . import comments, machine
from .client import LinearClient, WriteRefused
from .models import CommentView, GateObservation, IssueView
from .store import Store

__all__ = ["evaluate_gate", "enter_gate", "apply_gate_decision", "mark_unconfirmed",
           "REFUSALS", "DEGRADED_ACTOR", "UNREADABLE"]

CARD_MARKER = "**Poortkaart"
APPROVE_LABEL = "poort/akkoord"
REJECT_LABEL = "poort/afgekeurd"
WAITING_LABEL = "poort/wacht-op-mens"
FREE_LABEL = "poort/vrij"
UNCONFIRMED_LABEL = "run/onbevestigd"
HUMAN_REQUIRED_LABEL = "schakelaar/mens-vereist"
STUCK_LABEL = "run/vastgelopen"

REFUSALS = {
    1: "de actor staat niet op de goedkeurderslijst (D02, voorwaarde 1)",
    2: "de actor is een app-account en geen mens (D02, voorwaarde 2)",
    3: "de actor is het dispatcher-account zelf (D02, voorwaarde 3)",
    4: "het besluit is niet strikt nieuwer dan de poortkaart (D02, voorwaarde 4)",
    5: "de eerste regel is geen geldig poorttoken (D02, voorwaarde 5)",
    6: "er staat geen poortkaart van mij op dit issue, dus er valt niets te "
       "beantwoorden (D02, voorwaarde 4)",
}
DEGRADED_ACTOR = ("de actor van de labelwissel is niet vast te stellen uit "
                  "`Issue.history`; een label zonder actor kan van iedereen zijn")

#: Uitkomst van een waarneming die wél een poortsignaal is maar niet te lezen.
#: Zonder deze waarde valt zo'n comment door `outcome is None` uit de cyclus en
#: krijgt de mens die netjes antwoordde nooit iets terug.
UNREADABLE = "onleesbaar"
HIGH_RISK_LABEL_REFUSAL = (
    "risico/hoog vraagt om het geschreven token AKKOORD RISICO-GEZIEN; een labelklik draagt "
    "die risico-erkenning niet (spec 7.5, stap 3)"
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _find_card(comment_list: Sequence[CommentView]) -> Optional[CommentView]:
    cards = [c for c in comment_list if c.body.lstrip().startswith(CARD_MARKER)]
    return max(cards, key=lambda c: c.created_at) if cards else None


def _observation(issue: IssueView, card: Optional[CommentView], **kwargs) -> GateObservation:
    base = dict(
        issue_id=issue.id, gate_state=issue.state_name,
        card_comment_id=card.id if card else None,
        card_created_at=card.created_at if card else None,
        outcome=None, token=None, source=None, source_id=None,
        actor_id=None, actor_name=None, actor_is_app=None, valid=False, refusal=None,
        reason=None,
    )
    base.update(kwargs)
    return GateObservation(**base)


def evaluate_gate(client: LinearClient, issue: IssueView, *, approver_ids: frozenset[str],
                  dispatcher_user_id: str) -> GateObservation:
    """Kijkt of er een menselijk besluit ligt, en of dat besluit te vertrouwen is."""
    comment_list = client.comments(issue.id)
    card = _find_card(comment_list)

    decision = _read_comment_channel(issue, card, comment_list, approver_ids, dispatcher_user_id)
    if decision is not None:
        return decision
    return _read_label_channel(client, issue, card, approver_ids, dispatcher_user_id)


def _read_comment_channel(issue: IssueView, card: Optional[CommentView],
                          comment_list: Sequence[CommentView], approver_ids: frozenset[str],
                          dispatcher_user_id: str) -> Optional[GateObservation]:
    """Het tekstkanaal: het **nieuwste** besluit ná de poortkaart. None = geen besluit.

    Van achter naar voren, en alles wat ouder is dan de kaart telt niet mee.
    Andersom (het oudste besluit eerst) bevriest elk issue dat een poort twee
    keer bezoekt: de afkeuring van ronde 1 wordt dan gelezen als het antwoord op
    de kaart van ronde 2.
    """
    for comment in _after_the_card(card, comment_list):
        text = gate.strip_quotes_and_code(comment.body)
        try:
            parsed = gate.parse_gate_decision(text, high_risk=issue.high_risk)
        except gate.InvalidGateToken as exc:
            return _observation(
                issue, card, outcome=UNREADABLE, source="comment", source_id=comment.id,
                actor_id=comment.author_id, actor_name=comment.author_name,
                actor_is_app=comment.author_is_app,
                valid=False, refusal=f"{REFUSALS[5]}: {exc}",
            )
        if parsed is None:
            continue
        refusal = _check_conditions(comment.author_id, comment.author_is_app,
                                    comment.created_at, card, approver_ids, dispatcher_user_id)
        return _observation(
            issue, card, outcome=parsed.outcome, token=parsed.token, reason=parsed.reason,
            source="comment", source_id=comment.id, actor_id=comment.author_id,
            actor_name=comment.author_name,
            actor_is_app=comment.author_is_app, valid=refusal is None, refusal=refusal,
        )
    return None


def _after_the_card(card: Optional[CommentView],
                    comment_list: Sequence[CommentView]) -> list[CommentView]:
    """De comments die op deze poortronde slaan, nieuwste eerst.

    Zonder kaart blijft de hele lijst over: dan hoort de Spil een besluit wél te
    zíen, maar `_check_conditions` weigert het (voorwaarde 6). Stil negeren zou
    een mens laten wachten op een antwoord dat nooit komt.
    """
    newest_first = sorted(comment_list, key=lambda c: c.created_at, reverse=True)
    if card is None:
        return newest_first
    return [c for c in newest_first if c.created_at > card.created_at]


def _check_conditions(actor_id: Optional[str], actor_is_app: Optional[bool],
                      decided_at: Optional[datetime], card: Optional[CommentView],
                      approver_ids: frozenset[str], dispatcher_user_id: str) -> Optional[str]:
    """De voorwaarden 1 tot en met 4, in de volgorde van D02. None = alles goed."""
    if not actor_id or actor_id not in approver_ids:
        return REFUSALS[1]
    if actor_is_app is not False:
        return REFUSALS[2]
    if actor_id == dispatcher_user_id:
        return REFUSALS[3]
    if card is None:
        # Geen kaart betekent dat `enter_gate` halverwege is blijven steken of dat
        # dit besluit bij een eerdere poort hoorde. Beide gevallen zijn geen
        # toestemming, dus dit is een weigering en geen overgeslagen voorwaarde.
        return REFUSALS[6]
    if decided_at is None or decided_at <= card.created_at:
        return REFUSALS[4]
    return None


def _read_label_channel(client: LinearClient, issue: IssueView, card: Optional[CommentView],
                        approver_ids: frozenset[str],
                        dispatcher_user_id: str) -> GateObservation:
    """Het labelkanaal, even gezaghebbend als het tekstkanaal (spec 7.4)."""
    if APPROVE_LABEL in issue.labels:
        outcome, label = "akkoord", APPROVE_LABEL
    elif REJECT_LABEL in issue.labels:
        outcome, label = "afgekeurd", REJECT_LABEL
    else:
        return _observation(issue, card, valid=False, refusal=None)

    if outcome == "akkoord" and issue.high_risk:
        return _observation(issue, card, outcome=outcome, source="label", source_id=label,
                            valid=False, refusal=HIGH_RISK_LABEL_REFUSAL)

    actor = _label_actor(client, issue.id, label)
    if actor is None:
        # `Issue.history` geeft op deze workspace niets terug. Slot 2 sluit alleen
        # úít dat wij het label gezet hebben; elk ander account -- een collega, een
        # gast van de klant, een Codex- of Cursorsessie -- kan het net zo goed
        # geweest zijn. Zonder actor zijn voorwaarde 1 tot en met 4 niet te
        # controleren, dus gaat deze poort niet open.
        return _observation(issue, card, outcome=outcome, source="label", source_id=label,
                            valid=False, refusal=DEGRADED_ACTOR)
    actor_id, actor_name, actor_is_app, changed_at = actor
    refusal = _check_conditions(actor_id, actor_is_app, changed_at, card,
                                approver_ids, dispatcher_user_id)
    return _observation(issue, card, outcome=outcome, source="label", source_id=label,
                        actor_id=actor_id, actor_name=actor_name, actor_is_app=actor_is_app,
                        valid=refusal is None, refusal=refusal)


def _label_actor(client: LinearClient, issue_id: str,
                 label: str) -> Optional[tuple[str, str, bool, datetime]]:
    """De actor van de labelwissel uit `Issue.history`, of None als dat niet kan."""
    reader = getattr(client, "issue_history", None)
    if reader is None:
        return None
    nodes = reader(issue_id)
    if not nodes:
        return None
    leaf = label.split("/", 1)[1]
    for node in sorted(nodes, key=lambda n: n.get("createdAt") or "", reverse=True):
        added = node.get("addedLabels") or []
        if not any(entry.get("name") in (label, leaf) for entry in added):
            continue
        actor = node.get("actor") or {}
        if not actor.get("id"):
            return None
        moment = node.get("createdAt")
        parsed = (datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
                  if moment else _utcnow())
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return actor["id"], actor.get("name") or "", bool(actor.get("app")), parsed
    return None


def enter_gate(client: LinearClient, issue: IssueView, *, run_id: str, gate_state: str,
               approver_id: str, card_markdown: str, artefact_url: Optional[str]) -> None:
    """De zes handelingen van spec 7.2, altijd alle zes, altijd in deze volgorde."""
    client.update_issue(issue.id, run_id=run_id, state=gate_state,
                        current_state=issue.state_name, team_key=issue.team_key)
    client.update_issue(issue.id, run_id=run_id, assignee_id=approver_id, clear_delegate=True)
    removed = [FREE_LABEL] if FREE_LABEL in issue.labels else []
    if issue.run_state:
        removed.append(f"run/{issue.run_state}")
    client.update_issue(issue.id, run_id=run_id, added_labels=[WAITING_LABEL],
                        removed_labels=removed)
    client.update_issue(issue.id, run_id=run_id, priority=1)
    if artefact_url:
        client.attach_link(issue.id, artefact_url, f"Poortartefact {issue.identifier}",
                           run_id=run_id)
    client.create_comment(issue.id, card_markdown, run_id=run_id)


def apply_gate_decision(client: LinearClient, store: Store, issue: IssueView,
                        obs: GateObservation, *, run_id: str) -> Optional[str]:
    """Voert een geldig menselijk besluit uit. Geeft de nieuwe statusnaam terug.

    Weigert via `machine.assert_may_leave` zodra de waarneming niet deugt. Dat is
    slot 4: `update_issue` krijgt hier `gate_ok=True` mee en nergens anders.
    """
    machine.assert_may_leave(issue.state_name, obs)
    if obs.source_id and store.gate_decision_seen(issue.id, issue.state_name, obs.source_id):
        return None
    now = _utcnow()
    decided_at = _decided_at(client, issue, obs, now)

    if obs.outcome == "akkoord":
        return _approve(client, store, issue, obs, run_id=run_id, now=now,
                        decided_at=decided_at)
    return _reject(client, store, issue, obs, run_id=run_id, now=now, decided_at=decided_at)


def _decided_at(client: LinearClient, issue: IssueView, obs: GateObservation,
                fallback: datetime) -> datetime:
    """Wanneer de mens besloot, niet wanneer wij het zagen.

    Het verschil tussen poortkaart en besluit is de supervisiemeting (spec 7.5
    stap 8) en dat is een van de twee getallen die er echt toe doen; er zit tot
    een pollronde tussen. Bij het tekstkanaal is de comment-tijd het echte
    moment. Bij het labelkanaal weten we het niet en valt hij terug op nu.
    """
    if obs.source != "comment" or not obs.source_id:
        return fallback
    for comment in client.comments(issue.id):
        if comment.id == obs.source_id:
            return comment.created_at
    return fallback


def _approve(client: LinearClient, store: Store, issue: IssueView, obs: GateObservation, *,
             run_id: str, now: datetime, decided_at: datetime) -> Optional[str]:
    target = machine.next_state(issue.team_key, issue.state_name, "akkoord")
    if target is None:
        return None
    event_id = _record(store, issue, obs, decided_at, rejections=0)
    client.create_comment(issue.id, comments.confirmation_comment(
        run_id=run_id, when=now, actor_name=obs.actor_name or "een mens",
        decided_at=decided_at, source=obs.source or "comment",
        source_id=obs.source_id or "", outcome="akkoord", next_state=target), run_id=run_id)
    # Slot 2 verbiedt de Spil om `poort/akkoord` te zetten, ook als spec 7.5 stap 5
    # het als normalisatie beschrijft. Het bevestigingscomment hierboven is de
    # registratie; het label blijft van de mens.
    client.update_issue(
        issue.id, run_id=run_id, state=target, gate_ok=True,
        current_state=issue.state_name, team_key=issue.team_key,
        added_labels=[FREE_LABEL] if FREE_LABEL not in issue.labels else [],
        removed_labels=[label for label in (WAITING_LABEL, APPROVE_LABEL)
                        if label in issue.labels],
        clear_assignee=True,
    )
    store.mark_gate_applied(event_id, now)
    return target


def _reject(client: LinearClient, store: Store, issue: IssueView, obs: GateObservation, *,
            run_id: str, now: datetime, decided_at: datetime) -> Optional[str]:
    attempt = store.rejection_count(issue.id, issue.state_name) + 1
    reason = obs.reason or obs.token or "geen reden meegegeven"
    event_id = _record(store, issue, obs, decided_at, rejections=attempt)
    if attempt >= 2:
        # Spec 7.6 stap 7: geen derde poging. Het issue blijft in de poort staan.
        client.create_comment(issue.id, comments.stuck_comment(
            run_id=run_id, when=now, first_reason="zie de eerste afkeuring op deze poort",
            second_reason=reason), run_id=run_id)
        client.update_issue(issue.id, run_id=run_id,
                            added_labels=[STUCK_LABEL, WAITING_LABEL]
                            if WAITING_LABEL not in issue.labels else [STUCK_LABEL])
        store.mark_gate_applied(event_id, now)
        return None
    target = machine.next_state(issue.team_key, issue.state_name, "afgekeurd")
    if target is None:
        return None
    client.create_comment(issue.id, comments.rejection_comment(
        run_id=run_id, when=now, reason=reason, actor_name=obs.actor_name or "een mens",
        back_to=target, attempt=attempt), run_id=run_id)
    client.update_issue(
        issue.id, run_id=run_id, state=target, gate_ok=True,
        current_state=issue.state_name, team_key=issue.team_key,
        removed_labels=[label for label in (WAITING_LABEL, REJECT_LABEL) if label in issue.labels],
        clear_assignee=True,
    )
    store.mark_gate_applied(event_id, now)
    return target


def _record(store: Store, issue: IssueView, obs: GateObservation, decided_at: datetime,
            *, rejections: int) -> int:
    return store.record_gate_event(
        issue_id=issue.id, gate_state=issue.state_name, card_comment_id=obs.card_comment_id,
        card_at=obs.card_created_at, decided_at=decided_at, outcome=obs.outcome,
        token=obs.token,
        source=obs.source, source_id=obs.source_id, actor_id=obs.actor_id,
        actor_name=obs.actor_name, valid=obs.valid, refusal=obs.refusal, rejections=rejections,
    )


def mark_unconfirmed(client: LinearClient, store: Store, issue: IssueView,
                     obs: GateObservation, *, run_id: str) -> bool:
    """Zet het issue stil tot een mens het vrijgeeft. Geen statuswissel, geen run.

    Geeft False terug als deze bron al eerder geweigerd is. De weigering wordt
    net als een toepassing in `gate_events` vastgelegd, zodat dezelfde comment
    nooit twee keer een comment oplevert: bij `--loop --interval 60` was dat
    anders zestig identieke berichten per uur op precies het issue waar een mens
    naar zit te kijken.
    """
    if obs.valid:
        raise WriteRefused("mark_unconfirmed is voor ongeldige waarnemingen, niet voor geldige")
    if obs.source_id and store.gate_decision_seen(issue.id, issue.state_name, obs.source_id):
        return False
    now = _utcnow()
    event_id = _record(store, issue, obs, obs.card_created_at, rejections=0)
    client.create_comment(issue.id, comments.unconfirmed_comment(
        run_id=run_id, when=now, refusal=obs.refusal or "onbekende reden", source=obs.source,
        source_id=obs.source_id, actor_name=obs.actor_name), run_id=run_id)
    client.update_issue(
        issue.id, run_id=run_id,
        added_labels=[label for label in (UNCONFIRMED_LABEL, HUMAN_REQUIRED_LABEL)
                      if label not in issue.labels],
    )
    store.mark_gate_applied(event_id, now)
    return True
