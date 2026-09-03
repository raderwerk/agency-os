"""Hartslag en wachthond (spec 8.4).

Twee onafhankelijke dingen. De hartslag draait binnen de dispatcher: elke 15e
cyclus één comment op het bedieningspaneel met het aantal runs, de kosten van
vandaag en de lengte van de wachtrij, plus de tellers in de omschrijving.

De wachthond draait in een ánder proces (`*/10 * * * * python -m agency_os
heartbeat --watchdog`) en doet precies één ding: kijken of de laatste hartslag te
oud is. Zo ja, dan zet hij `schakelaar/motor-dood` op het paneel en schrijft hij
één comment. Meer niet, ooit. Wie de wachter bewaakt is een open vraag en staat
zo ook in het eerlijkheidsdocument.

Van de Store gebruikt deze module: `last_heartbeat_at()`, `record_heartbeat(...)`
en `get_meta(key, default)` (tabellen `heartbeats` en `meta` uit spec 5.1).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from agency_os.linear import comments, ledger
from agency_os.linear.client import LinearError

DEAD_LABEL = "schakelaar/motor-dood"
COUNTER_START = "<!-- spil:tellers -->"
COUNTER_END = "<!-- /spil:tellers -->"

ALIVE, TRIPPED, UNKNOWN = 0, 1, 2


def due(cycle_index: int, every: int) -> bool:
    """Waar op elke `every`-de cyclus. Cycli tellen vanaf 1."""
    return every > 0 and cycle_index > 0 and cycle_index % every == 0


def beat(client: Any, store: Any, cfg: Any, panel: Any, *, run_id: str) -> None:
    """Schrijft de hartslag op het bedieningspaneel en werkt de tellers bij."""
    now = datetime.now(timezone.utc)
    day = now.date()
    rollup = ledger.rollup(store, day)
    queue_len = int(store.get_meta("queue_len", "0") or 0)
    cycle = int(store.get_meta("cycle", "0") or 0)

    body = "\n".join(
        [
            comments.signature("Spil", "dispatcher", run_id, now),
            "",
            f"Hartslag. Vandaag {rollup.runs} runs op {rollup.issues} issues, "
            f"€ {rollup.eur:.2f} aan modelkosten, {queue_len} issues in de wachtrij.",
            "",
            f"Issueteller: {rollup.issue_count} van de 250. Poorten vandaag: "
            f"{rollup.gates_passed} akkoord, {rollup.gates_rejected} afgekeurd.",
        ]
    )
    comment_id = client.create_comment(panel.id, body, run_id=run_id)

    description = _with_counters(panel.description or "", rollup=rollup, queue_len=queue_len, now=now)
    if description is not None:
        client.update_issue(panel.id, run_id=run_id, description=description)

    store.record_heartbeat(
        at=now,
        cycle=cycle,
        comment_id=comment_id,
        runs_today=rollup.runs,
        cost_eur_today=rollup.eur,
        queue_len=queue_len,
    )


def watchdog(client: Any, store: Any, cfg: Any) -> int:
    """Kijkt of de dispatcher nog leeft. 0 leeft, 1 stop gezet, 2 niet vast te stellen."""
    run_id = secrets.token_hex(3)
    try:
        panel = client.issue(cfg.panel_identifier)
    except LinearError as exc:
        if exc.matches("401", "authentication", "AUTHENTICATION_ERROR"):
            return TRIPPED
        return UNKNOWN
    except Exception:  # netwerk, dns, timeout: onbekend is niet hetzelfde als dood
        return UNKNOWN

    last = store.last_heartbeat_at()
    if last is None:
        return UNKNOWN
    now = datetime.now(timezone.utc)
    age_s = (now - last).total_seconds()
    if age_s <= cfg.watchdog_max_age_s:
        return ALIVE
    if DEAD_LABEL in panel.labels:
        return TRIPPED  # al gemeld; de wachthond schrijft nooit een tweede keer

    client.update_issue(panel.id, run_id=run_id, added_labels=[DEAD_LABEL])
    client.create_comment(
        panel.id,
        "\n".join(
            [
                comments.signature("Wachthond", "cron", run_id, now),
                "",
                f"De laatste hartslag is van {last:%Y-%m-%d %H:%M} UTC, {int(age_s // 60)} minuten geleden, "
                f"terwijl er hooguit {cfg.watchdog_max_age_s // 60} minuten tussen mogen zitten. "
                "Ik ga ervan uit dat de dispatcher niet meer draait en zet motor-dood. "
                "Ik doe verder niets: starten en stoppen is mensenwerk.",
            ]
        ),
        run_id=run_id,
    )
    return TRIPPED


def _with_counters(description: str, *, rollup: Any, queue_len: int, now: datetime) -> Optional[str]:
    """Vervangt het tellerblok in de paneelomschrijving, of laat alles staan.

    Zonder de twee markeringen raakt deze functie de omschrijving niet aan: een
    hartslag mag nooit tekst van een mens overschrijven.
    """
    start = description.find(COUNTER_START)
    end = description.find(COUNTER_END)
    if start < 0 or end < start:
        return None
    block = "\n".join(
        [
            COUNTER_START,
            f"Laatste hartslag: {now:%Y-%m-%d %H:%M} UTC",
            f"Runs vandaag: {rollup.runs} · kosten vandaag: € {rollup.eur:.2f} · wachtrij: {queue_len}",
            f"Issueteller: {rollup.issue_count} van 250",
        ]
    )
    return description[:start] + block + "\n" + description[end:]
