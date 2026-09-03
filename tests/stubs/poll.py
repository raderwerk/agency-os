"""Stand-in voor `agency_os.linear.poll` (onderdeel A), contract 3.4 en spec 8.1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from agency_os.linear import killswitch
from agency_os.linear.models import PollResult

BUSY = "run/bezet"


@dataclass(frozen=True)
class PollConfig:
    team_keys: tuple[str, ...]
    panel_identifier: str
    in_scope_states: Mapping[str, tuple[str, ...]]
    max_claims: int


def poll(client, cfg: PollConfig) -> PollResult:
    issues = [i for i in client.all_issues() if i.team_key in cfg.team_keys]
    panel = next((i for i in issues if i.identifier == cfg.panel_identifier), None)
    switches = killswitch.read_switches(
        client, panel, issues, issue_count=client.organization_issue_count(), thresholds=(200, 220, 225)
    )
    ready, gates, watching, skipped = [], [], [], []
    for issue in issues:
        if panel is not None and issue.id == panel.id:
            continue
        if issue.is_gate_state:
            gates.append(issue)
        elif BUSY in issue.labels:
            watching.append(issue)
        elif issue.state_name in cfg.in_scope_states.get(issue.team_key, ()):
            ready.append(issue)
        else:
            skipped.append((issue.identifier, "buiten-mvp"))
    ready.sort(key=lambda i: (i.priority or 9, i.updated_at))
    return PollResult(at=datetime.now(timezone.utc), panel=panel, switches=switches,
                      ready=tuple(ready), gates=tuple(gates), watching=tuple(watching), skipped=tuple(skipped))
