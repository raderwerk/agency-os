"""Stand-in voor `agency_os.linear.gates` (onderdeel A), contract 3.4 en spec 7.

De vijf poortvoorwaarden staan hier echt in, want de planner-test moet kunnen
laten zien dat een ongeldig akkoord het issue stopzet in plaats van doorlaat.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from agency_os import gate
from agency_os.linear import comments, machine
from agency_os.linear.models import GateObservation

CARD_MARKER = "Poortkaart"
_FENCE = re.compile(r"```.*?```", re.S)


def _strip(text: str) -> str:
    stripper = getattr(gate, "strip_quotes_and_code", None)
    if stripper is not None:
        return stripper(text)
    without_code = _FENCE.sub("", text or "")
    return "\n".join(line for line in without_code.splitlines() if not line.lstrip().startswith(">"))


def evaluate_gate(client, issue, *, approver_ids: frozenset[str], dispatcher_user_id: str) -> GateObservation:
    history = client.comments(issue.id)
    card = next((c for c in reversed(history) if c.author_id == dispatcher_user_id and CARD_MARKER in c.body), None)
    base = dict(
        issue_id=issue.id,
        gate_state=issue.state_name,
        card_comment_id=card.id if card else None,
        card_created_at=card.created_at if card else None,
    )
    for comment in reversed(history):
        try:
            decision = gate.parse_gate_decision(_strip(comment.body), high_risk=issue.high_risk)
        except gate.InvalidGateToken as exc:
            return GateObservation(**base, outcome=None, token=None, source="comment", source_id=comment.id,
                                   actor_id=comment.author_id, actor_name=comment.author_name,
                                   actor_is_app=comment.author_is_app, valid=False, refusal=str(exc))
        if decision is None:
            continue
        refusal = _refusal(comment, card, approver_ids, dispatcher_user_id)
        return GateObservation(**base, outcome=decision.outcome, token=decision.token, source="comment",
                               source_id=comment.id, actor_id=comment.author_id, actor_name=comment.author_name,
                               actor_is_app=comment.author_is_app, valid=refusal is None, refusal=refusal)
    return GateObservation(**base, outcome=None, token=None, source=None, source_id=None, actor_id=None,
                           actor_name=None, actor_is_app=None, valid=False, refusal=None)


def _refusal(comment, card, approver_ids, dispatcher_user_id) -> Optional[str]:
    if comment.author_id not in approver_ids:
        return "voorwaarde 1: de auteur staat niet op de goedkeurderslijst"
    if comment.author_is_app:
        return "voorwaarde 2: de auteur is een app-user"
    if comment.author_id == dispatcher_user_id:
        return "voorwaarde 3: de auteur is het dispatcher-account"
    if card is not None and comment.created_at <= card.created_at:
        return "voorwaarde 4: het antwoord is niet nieuwer dan de poortkaart"
    return None


def enter_gate(client, issue, *, run_id: str, gate_state: str, approver_id: str,
               card_markdown: str, artefact_url: Optional[str]) -> None:
    """De zes handelingen van spec 7.2, in deze volgorde."""
    client.update_issue(issue.id, run_id=run_id, state=gate_state)
    client.update_issue(issue.id, run_id=run_id, assignee_id=approver_id, clear_delegate=True)
    client.update_issue(issue.id, run_id=run_id, added_labels=["poort/wacht-op-mens"],
                        removed_labels=["poort/vrij", "run/bezet"])
    client.update_issue(issue.id, run_id=run_id, priority=1)
    if artefact_url:
        client.attach_link(issue.id, artefact_url, "Artefact van de poort", run_id=run_id)
    client.create_comment(issue.id, card_markdown, run_id=run_id)


def apply_gate_decision(client, store, issue, obs: GateObservation, *, run_id: str) -> Optional[str]:
    machine.assert_may_leave(issue.state_name, obs)
    target = machine.next_state(issue.team_key, issue.state_name, obs.outcome)
    signature = comments.signature("Spil", "dispatcher", run_id, datetime.now(timezone.utc))
    if obs.outcome == "akkoord":
        body = f"{signature}\n\nGoedgekeurd door {obs.actor_name} ({obs.source} {obs.source_id})."
    else:
        body = f"{signature}\n\nAfgekeurd door {obs.actor_name}: {obs.token}."
    client.create_comment(issue.id, body, run_id=run_id)
    client.update_issue(issue.id, run_id=run_id, state=target, gate_ok=True,
                        added_labels=["poort/vrij"], removed_labels=["poort/wacht-op-mens"],
                        assignee_id=None)
    return target


def mark_unconfirmed(client, issue, obs: GateObservation, *, run_id: str) -> None:
    client.update_issue(issue.id, run_id=run_id,
                        added_labels=["run/onbevestigd", "schakelaar/mens-vereist"])
    client.create_comment(
        issue.id,
        comments.signature("Spil", "dispatcher", run_id, datetime.now(timezone.utc))
        + f"\n\nIk zie een poortbeslissing die ik niet mag volgen: {obs.refusal}. "
        "Dit issue blijft staan tot een mens het vrijgeeft.",
        run_id=run_id,
    )
