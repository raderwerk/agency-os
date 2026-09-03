"""Stand-in voor `agency_os.linear.killswitch` (onderdeel A), contract 3.4 en spec 8.5."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Optional, Sequence

from agency_os.linear import comments
from agency_os.linear.models import SwitchState

PAUSE_ALL = "schakelaar/pauze-alles"
PAUSE = "schakelaar/pauze"
DEAD = "schakelaar/motor-dood"


def read_switches(client, panel, issues: Sequence, *, issue_count: int, thresholds) -> SwitchState:
    warn, restrict, stop = thresholds
    level = "ok"
    reason: Optional[str] = None
    if issue_count >= stop:
        level, reason = "stop", f"issueteller {issue_count} boven {stop}"
    elif issue_count >= restrict:
        level, reason = "restrict", f"issueteller {issue_count} boven {restrict}"
    elif issue_count >= warn:
        level, reason = "warn", f"issueteller {issue_count} boven {warn}"
    labels = panel.labels if panel is not None else ()
    if PAUSE_ALL in labels:
        reason = "schakelaar/pauze-alles staat op het bedieningspaneel"
    return SwitchState(
        global_pause=PAUSE_ALL in labels,
        paused_issue_ids=frozenset(i.id for i in issues if PAUSE in i.labels),
        engine_dead=DEAD in labels,
        issue_count=issue_count,
        budget_level=level,
        reason=reason,
    )


def halt_everything(client, store, switches: SwitchState, *, run_id: str) -> int:
    aborted = 0
    for row in store.open_claims():
        client.update_issue(row["issue_id"], run_id=run_id,
                            added_labels=["run/wachtrij"], removed_labels=["run/bezet"])
        store.close_claim(row["issue_id"], row["run_id"], "afgebroken", datetime.now(timezone.utc))
        aborted += 1
    return aborted


def trip_emergency_stop(client, panel, reason: str, *, run_id: str) -> None:
    client.update_issue(panel.id, run_id=run_id, added_labels=[PAUSE_ALL])
    client.create_comment(
        panel.id,
        comments.signature("Spil", "dispatcher", run_id or secrets.token_hex(3), datetime.now(timezone.utc))
        + f"\n\nIk zet de noodstop aan: {reason}. Uitzetten is mensenwerk.",
        run_id=run_id,
    )
