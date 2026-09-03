"""Poortlogica: het lezen van een menselijk akkoord- of afkeurtoken.

Zie hq/design/linear-workspace-spec.md sectie 2.3, 3.4, 3.5 en de lijst "Wat
geen enkele rol ooit mag" (punt 2). Een agent mag nooit zelf een comment
schrijven waarvan de eerste regel met AKKOORD of AFGEKEURD begint -- dat is
uitsluitend voorbehouden aan een mens bij een poortstatus. Bij `risico/hoog`
moet het akkoordtoken bovendien letterlijk `AKKOORD RISICO-GEZIEN` luiden.

Deze module is het enige stuk code dat weet wat een geldig token is, zodat de
regel niet los in elke rol terugkomt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

AKKOORD = "AKKOORD"
AFGEKEURD = "AFGEKEURD"
HIGH_RISK_TOKEN = "AKKOORD RISICO-GEZIEN"


class InvalidGateToken(ValueError):
    """De eerste regel begint wel met AKKOORD, maar voldoet niet aan de eisen
    die voor dit issue gelden (bijvoorbeeld risico/hoog)."""


@dataclass(frozen=True)
class GateDecision:
    outcome: str  # "akkoord" of "afgekeurd"
    token: str


def _first_line(comment_text: Optional[str]) -> str:
    if not comment_text or not comment_text.strip():
        return ""
    return comment_text.strip().splitlines()[0].strip()


def parse_gate_decision(comment_text: str, *, high_risk: bool = False) -> Optional[GateDecision]:
    """Leest de eerste regel van een poortcomment.

    Geeft `None` als de comment geen akkoord- of afkeurtoken is (dus geen
    poortbeslissing). Gooit `InvalidGateToken` als de eerste regel wel met
    AKKOORD begint maar het verkeerde token is voor dit issue.
    """
    first_line = _first_line(comment_text)

    if first_line == AFGEKEURD:
        return GateDecision(outcome="afgekeurd", token=first_line)

    if first_line == HIGH_RISK_TOKEN:
        return GateDecision(outcome="akkoord", token=first_line)

    if first_line == AKKOORD:
        if high_risk:
            raise InvalidGateToken(
                f"risico/hoog vereist het exacte token {HIGH_RISK_TOKEN!r}, kreeg {first_line!r}"
            )
        return GateDecision(outcome="akkoord", token=first_line)

    if first_line.startswith(AKKOORD) or first_line.startswith(AFGEKEURD):
        raise InvalidGateToken(f"onbekend poorttoken: {first_line!r}")

    return None


def is_gate_opening_comment(comment_text: str) -> bool:
    """True als de eerste regel met AKKOORD of AFGEKEURD begint."""
    first_line = _first_line(comment_text)
    return first_line.startswith(AKKOORD) or first_line.startswith(AFGEKEURD)


def assert_not_gate_opening(comment_text: str, *, author_is_agent: bool) -> None:
    """Harde controle vlak voor elke schrijfactie van een agent.

    Roep dit aan met de tekst die een agent op het punt staat te plaatsen. Een
    mens mag dit token altijd schrijven; een agent nooit (rolcontract Spil,
    verbod 2; "Wat geen enkele rol ooit mag", punt 2).
    """
    if author_is_agent and is_gate_opening_comment(comment_text):
        raise InvalidGateToken(
            "een agent mag geen comment schrijven waarvan de eerste regel met "
            f"{AKKOORD!r} of {AFGEKEURD!r} begint"
        )
