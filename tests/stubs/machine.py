"""Stand-in voor `agency_os.linear.machine` (onderdeel A), contract 3.4 en spec 7.

De drie tabellen staan hier letterlijk zoals in de architectuur; geen enkele
andere plek in de codebase mag een status bepalen.
"""

from __future__ import annotations

from typing import Optional

from agency_os.linear.client import WriteRefused

GATE_PREFIX = "Poort"

NEXT_ON_DONE = {
    ("WV", "Ingepland"): "In uitvoering",
    ("WV", "In uitvoering"): "Agentreview",
    ("WV", "Agentreview"): "QA op preview",
    ("WV", "QA op preview"): "Poort · Merge of publicatie",
    ("WV", "Na-merge controle"): "Klaar",
    ("KR", "Lead"): "Gekwalificeerd",
    ("KR", "Discovery"): "Voorstel",
    ("KR", "Voorstel"): "Poort 1 · Voorstel akkoord",
}
GATE_ON_APPROVE = {
    ("WV", "Poort · Merge of publicatie"): "Na-merge controle",
    ("KR", "Poort 1 · Voorstel akkoord"): "Kickoff",
    ("KR", "Poort 2 · Oplevering akkoord"): "Poort 3 · Factuur akkoord",
    ("KR", "Poort 3 · Factuur akkoord"): "Afgerond",
}
GATE_ON_REJECT = {
    ("WV", "Poort · Merge of publicatie"): "In uitvoering",
    ("KR", "Poort 1 · Voorstel akkoord"): "Voorstel",
    ("KR", "Poort 2 · Oplevering akkoord"): "Klantacceptatie",
    ("KR", "Poort 3 · Factuur akkoord"): "Poort 2 · Oplevering akkoord",
}
WAIT_STATE = {"WV": "Wacht op input", "KR": "Wacht op input"}


def is_gate(state_name: str) -> bool:
    return bool(state_name) and state_name.startswith(GATE_PREFIX)


def next_state(team_key: str, state_name: str, outcome: str) -> Optional[str]:
    table = {"klaar": NEXT_ON_DONE, "akkoord": GATE_ON_APPROVE, "afgekeurd": GATE_ON_REJECT}[outcome]
    return table.get((team_key, state_name))


def assert_may_leave(state_name: str, obs) -> None:
    if not is_gate(state_name):
        return
    if obs is None or not obs.valid or obs.outcome is None:
        raise WriteRefused(f"{state_name} verlaten mag alleen met een geldige poortwaarneming")
