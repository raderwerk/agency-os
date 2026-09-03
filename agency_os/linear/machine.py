"""De statusovergangen als data, plus drie pure opzoekfuncties.

Nergens anders in de codebase beslist een `if`-keten over een status. Wie wil
weten waar een issue heen gaat, leest deze drie tabellen -- of, als het antwoord
er niet in staat, verplaatst het issue niet.
"""

from __future__ import annotations

from typing import Mapping, Optional

from .client import WriteRefused
from .models import GateObservation

__all__ = [
    "GATE_PREFIX",
    "NEXT_ON_DONE",
    "GATE_ON_APPROVE",
    "GATE_ON_REJECT",
    "WAIT_STATE",
    "is_gate",
    "next_state",
    "assert_may_leave",
]

GATE_PREFIX = "Poort"

NEXT_ON_DONE: Mapping[tuple[str, str], str] = {
    ("WV", "Ingepland"): "In uitvoering",
    ("WV", "In uitvoering"): "Agentreview",
    ("WV", "Agentreview"): "QA op preview",
    ("WV", "QA op preview"): "Poort · Merge of publicatie",
    ("WV", "Na-merge controle"): "Klaar",
    ("KR", "Lead"): "Gekwalificeerd",
    ("KR", "Discovery"): "Voorstel",
    ("KR", "Voorstel"): "Poort 1 · Voorstel akkoord",
}

GATE_ON_APPROVE: Mapping[tuple[str, str], str] = {
    ("WV", "Poort · Merge of publicatie"): "Na-merge controle",
    ("KR", "Poort 1 · Voorstel akkoord"): "Kickoff",
    ("KR", "Poort 2 · Oplevering akkoord"): "Poort 3 · Factuur akkoord",
    ("KR", "Poort 3 · Factuur akkoord"): "Afgerond",
}

GATE_ON_REJECT: Mapping[tuple[str, str], str] = {
    ("WV", "Poort · Merge of publicatie"): "In uitvoering",
    ("KR", "Poort 1 · Voorstel akkoord"): "Voorstel",
    ("KR", "Poort 2 · Oplevering akkoord"): "Klantacceptatie",
    ("KR", "Poort 3 · Factuur akkoord"): "Poort 2 · Oplevering akkoord",
}

WAIT_STATE: Mapping[str, str] = {"WV": "Wacht op input", "KR": "Wacht op input"}

_OUTCOME_TABLES = {
    "klaar": NEXT_ON_DONE,
    "akkoord": GATE_ON_APPROVE,
    "afgekeurd": GATE_ON_REJECT,
}


def is_gate(state_name: str) -> bool:
    """Een poortstatus is elke status waarvan de naam met `Poort` begint."""
    return bool(state_name) and state_name.startswith(GATE_PREFIX)


def next_state(team_key: str, state_name: str, outcome: str) -> Optional[str]:
    """De volgende status, of None als er geen overgang bestaat.

    `outcome` is `klaar`, `akkoord`, `afgekeurd` of `vraag`. Bij `mislukt` en
    `afgebroken` blijft een issue staan, dus die geven None terug.
    """
    if outcome == "vraag":
        return WAIT_STATE.get(team_key)
    table = _OUTCOME_TABLES.get(outcome)
    if table is None:
        return None
    return table.get((team_key, state_name))


def assert_may_leave(state_name: str, obs: Optional[GateObservation]) -> None:
    """Het enige punt dat toestemming geeft om een poortstatus te verlaten.

    Geen geldige waarneming, geen vertrek. Dit is slot 4 van de drie
    onafhankelijke poortsloten (architectuur 8); `client.update_issue` vraagt om
    `gate_ok=True` en alleen `gates.apply_gate_decision` zet dat na deze
    controle.
    """
    if not is_gate(state_name):
        return
    if obs is None:
        raise WriteRefused(
            f"{state_name!r} verlaten mag niet zonder waarneming van een menselijk besluit"
        )
    if not obs.valid:
        raise WriteRefused(
            f"{state_name!r} verlaten geweigerd: de waarneming is ongeldig "
            f"({obs.refusal or 'geen reden vastgelegd'})"
        )
    if obs.outcome not in ("akkoord", "afgekeurd"):
        raise WriteRefused(
            f"{state_name!r} verlaten geweigerd: er is nog geen besluit van een mens"
        )
