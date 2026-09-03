"""Het claimprotocol. Een status is geen slot, dus dit is een benadering.

Spec 8.2 en architectuur 13. Vier lagen op elkaar:

1. `run/bezet` in Linear -- het signaal naar buiten, zichtbaar op het bord.
2. Een claimcomment met het run-id, 5 seconden wachten, terugleggen: bij twee
   claimers wint het laagste run-id en zet de verliezer alles terug wat hij zelf
   heeft aangezet -- het label gaat terug naar `run/wachtrij`, het claimcomment
   blijft staan als spoor.
3. De unieke index `one_open_claim_per_issue` in sqlite -- de echte garantie
   binnen dit proces.
4. Idempotentie: voordat er een comment komt wordt gecontroleerd of er al een
   comment met dit run-id staat, zodat een herstarte run nooit dubbel schrijft.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from . import comments
from .client import LinearClient
from .models import Claim, IssueView
from .store import Store

__all__ = ["try_claim", "release_claim", "already_ran", "existing_run_comment",
           "FINAL_LABELS", "BUSY_LABEL", "QUEUE_LABEL"]

BUSY_LABEL = "run/bezet"
QUEUE_LABEL = "run/wachtrij"
FINAL_LABELS = frozenset({"run/klaar", "run/mislukt", "run/wachtrij", "run/vastgelopen"})

_CLAIM_RE = re.compile(r"\*\*Spil\*\*\s+claim\s+([0-9a-f]{6})\b")
_RUN_RE = re.compile(r"\b(?:run|claim)\s+([0-9a-f]{6})\b")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def already_ran(store: Store, issue_id: str, run_id: str) -> bool:
    """True als dit run-id dit issue al geclaimd heeft (herstart-detectie)."""
    return store.has_claim(issue_id, run_id)


def existing_run_comment(client: LinearClient, issue_id: str, run_id: str) -> Optional[str]:
    """Het id van een comment dat dit run-id al draagt, of None.

    Dit is de idempotentiecontrole van spec 8.2: een herstarte run schrijft geen
    tweede comment.
    """
    for comment in client.comments(issue_id):
        match = _RUN_RE.search(comment.body)
        if match and match.group(1) == run_id:
            return comment.id
    return None


def try_claim(client: LinearClient, store: Store, issue: IssueView, run_id: str, *,
              settle_s: float = 5.0,
              now: Callable[[], datetime] = _utcnow) -> Optional[Claim]:
    """Claimt één issue, of geeft None als het niet van ons is.

    None betekent altijd: niet aanraken. Het kan zijn dat een ander proces het
    heeft, dat er al een openstaande claim in sqlite staat, of dat wij het
    settle-venster verloren hebben.
    """
    open_claim = store.open_claim(issue.id)
    if open_claim is not None and open_claim["run_id"] != run_id:
        return None
    if open_claim is None and not store.insert_claim(
            issue.id, run_id, issue.identifier, now()):
        return None

    we_set_the_label = issue.run_state != "bezet"
    if we_set_the_label:
        client.update_issue(
            issue.id, run_id=run_id, added_labels=[BUSY_LABEL],
            removed_labels=[QUEUE_LABEL] if issue.run_state == "wachtrij" else [],
        )

    claimed_at = now()
    comment_id = existing_run_comment(client, issue.id, run_id)
    if comment_id is None:
        comment_id = client.create_comment(
            issue.id, comments.claim_comment(run_id, claimed_at), run_id=run_id)

    if settle_s > 0:
        time.sleep(settle_s)

    if _lost_the_settle_window(client, store, issue.id, run_id, claimed_at, settle_s):
        # Spec 8.2 stap 4: de verliezer trekt zich terug. Het claimcomment blijft
        # staan -- dat is de afspraak -- maar het label gaat terug naar
        # `run/wachtrij`. Een achtergelaten `run/bezet` zonder open claim maakt
        # het issue onclaimbaar: de poll zet het daarna in `watching` en niemand
        # pakt het ooit nog op.
        if we_set_the_label:
            client.update_issue(
                issue.id, run_id=run_id, added_labels=[QUEUE_LABEL],
                removed_labels=[BUSY_LABEL],
            )
        store.release_claim(issue.id, run_id, "verloren", now())
        return None

    return Claim(run_id=run_id, issue_id=issue.id, issue_identifier=issue.identifier,
                 claimed_at=claimed_at, comment_id=comment_id)


def _lost_the_settle_window(client: LinearClient, store: Store, issue_id: str, run_id: str,
                            claimed_at: datetime, settle_s: float) -> bool:
    """True als er binnen het venster een levende claim met een lager run-id staat.

    Twee vernauwingen ten opzichte van "elk claimcomment telt": het venster
    loopt één settle-tijd terug in plaats van twee, en een run die zijn claim
    al heeft losgelaten is geen tegenstander meer. Zonder die twee verliest de
    tweede `run --once` uit het leesmij-recept het venster van zijn eigen
    voorganger, met de helft kans op een lager run-id.
    """
    window_start = claimed_at.timestamp() - max(settle_s, 1.0)
    for comment in client.comments(issue_id):
        match = _CLAIM_RE.search(comment.body)
        if not match:
            continue
        rival = match.group(1)
        if rival >= run_id:  # gelijk aan onszelf, of een hoger id dat verliest
            continue
        if comment.created_at.timestamp() < window_start:
            continue
        if store.claim_is_closed(issue_id, rival):
            continue
        return True
    return False


def release_claim(client: LinearClient, store: Store, claim: Claim, *, final_label: str) -> None:
    """Sluit de claim af en zet het run-label naar zijn eindwaarde."""
    if final_label not in FINAL_LABELS:
        raise ValueError(
            f"{final_label!r} is geen eindlabel; kies uit {sorted(FINAL_LABELS)}"
        )
    store.release_claim(claim.issue_id, claim.run_id, final_label.split("/")[-1])
    client.update_issue(claim.issue_id, run_id=claim.run_id, added_labels=[final_label],
                        removed_labels=[BUSY_LABEL])
