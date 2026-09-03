"""Elke commentvorm die de Spil schrijft, als pure rendering.

Geen enkele functie hier schrijft iets weg; ze geven markdown terug die de
aanroeper via `client.create_comment` plaatst. Dat is bewust: het schrijfslot
tegen poortopenende comments zit in de client, en niet in dit bestand.

Tijden in comments staan in Europe/Amsterdam, formaat `2026-09-03 11:14`
(architectuur 3.1). De rest van de codebase rekent in UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from .models import Artifact, RunRecord

__all__ = [
    "AMSTERDAM",
    "local_time",
    "signature",
    "claim_comment",
    "run_comment",
    "gate_card",
    "qa_report",
    "halt_comment",
    "native_fallback_comment",
    "confirmation_comment",
    "rejection_comment",
    "stuck_comment",
    "unconfirmed_comment",
    "evidence_block",
]

AMSTERDAM = ZoneInfo("Europe/Amsterdam")


def local_time(when: datetime) -> str:
    """UTC -> `2026-09-03 11:14` in Amsterdamse tijd."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(AMSTERDAM).strftime("%Y-%m-%d %H:%M")


def _euro(amount: float) -> str:
    return ("€ %.2f" % amount).replace(".", ",")


def signature(role_title: str, model_display: str, run_id: str, when: datetime) -> str:
    """`**Ontwikkelaar · Claude Opus 5 · run 3f9a2c · 2026-09-03 11:14**` (spec 8.3)."""
    return f"**{role_title} · {model_display} · run {run_id} · {local_time(when)}**"


def claim_comment(run_id: str, when: datetime) -> str:
    """De claimregel van spec 8.2 stap 1. Bewust één regel: hij wordt teruggelezen."""
    return f"**Spil** claim {run_id} op {local_time(when)}"


def evidence_block(evidence: Sequence[Artifact]) -> str:
    if not evidence:
        return "**Bewijs** geen — er is niets opgeleverd om naar te linken."
    lines = ["**Bewijs**"]
    for item in evidence:
        label = item.label or item.type
        lines.append(f"- {label}: {item.url}")
    return "\n".join(lines)


def run_comment(*, role_title: str, model_display: str, run: RunRecord, body_md: str,
                evidence: Sequence[Artifact], dod: str, next_state: str) -> str:
    """Het uitvoercontract van spec 8.3: handtekening, proza, bewijs, DoD, status, staartblok."""
    from .ledger import render_tail_block  # laat in de functie: ledger leest comments terug

    return "\n\n".join([
        signature(role_title, model_display, run.run_id, run.gestart),
        body_md.strip(),
        evidence_block(evidence),
        f"**Definition of Done** {dod}",
        f"**Volgende status** {next_state}",
        render_tail_block(run),
    ])


def gate_card(*, gate_no: str, issue, what: str, evidence: Sequence[Artifact], criteria: str,
              reviewers: str, disagreement: str, risk: str, cost_so_far: str, high_risk: bool,
              run_id: str, duration_s: float, cost_eur: float) -> str:
    """De poortkaart van spec 7.3.

    De tokens staan midden in de tekst op eigen regels, nooit op regel 1: een
    kaart is een instructie aan een mens, geen poortbesluit. Het schrijfslot in
    `client.create_comment` controleert dat ook nog een keer.
    """
    answer = (
        "AKKOORD RISICO-GEZIEN\nAFGEKEURD: <reden>" if high_risk else "AKKOORD\nAFGEKEURD: <reden>"
    )
    risk_line = f"**Risico** {risk}"
    if high_risk:
        risk_line += (
            "\n\nDit issue draagt `risico/hoog`. Een kaal AKKOORD wordt geweigerd: het token "
            "moet letterlijk AKKOORD RISICO-GEZIEN zijn, zodat jouw risico-erkenning op de rol "
            "staat."
        )
    return "\n\n".join([
        f"**Poortkaart {gate_no} · {issue.identifier}**",
        f"**Waar je ja tegen zegt** {what}",
        evidence_block(evidence),
        f"**Acceptatiecriteria** {criteria}",
        f"**Reviewers** {reviewers}",
        f"**Oneens** {disagreement}",
        risk_line,
        f"**Kosten tot nu** {cost_so_far}",
        "**Hoe je antwoordt** zet het label `poort/akkoord` of `poort/afgekeurd`, of plaats een "
        "comment waarvan de eerste regel exact is:\n" + answer,
        f"— Raderwerk · Spil (dispatcher) · run {run_id} · {duration_s:.0f}s · "
        f"{_euro(cost_eur)}",
    ])


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def qa_report(*, model_display: str, run_id: str, when: datetime, verdict: str, tested: str,
              criteria_rows: Sequence[tuple[str, str, str, str]], suite_ran: bool,
              suite_output: str, findings_rows: Sequence[tuple[str, str, str, str]],
              edge_cases: str, not_verified: str, regression_risk: str) -> str:
    """Het QA-rapport van spec 5.9, letterlijk dat sjabloon."""
    return "\n\n".join([
        signature("QA", model_display, run_id, when),
        f"**Oordeel** {verdict}",
        f"**Wat is getest** {tested}",
        "**Acceptatiecriteria**\n"
        + _table(("#", "Criterium", "Uitkomst", "Bewijs"), criteria_rows),
        f"**Testsuite** volledig gedraaid: {'ja' if suite_ran else 'nee'}. Uitvoer:\n\n"
        f"```\n{suite_output.strip()}\n```",
        "**Bevindingen**\n" + _table(("Ernst", "Bevinding", "Waar", "Voorstel"), findings_rows),
        f"**Randgevallen** {edge_cases}",
        f"**Wat ik niet heb kunnen controleren** {not_verified}",
        f"**Regressierisico** {regression_risk}",
    ])


def halt_comment(run_id: str, when: datetime, aborted: int, elapsed_s: float,
                 cost_eur: float) -> str:
    """Het ene comment op het bedieningspaneel na een noodstop (spec 8.5)."""
    return "\n\n".join([
        signature("Spil (dispatcher)", "geen model", run_id, when),
        f"Noodstop gelezen. Ik heb {aborted} lopende run(s) afgebroken en elk `run/bezet` "
        f"teruggezet op `run/wachtrij`. Tijd sinds de omschakeling: {elapsed_s / 60:.0f} minuten. "
        f"Kosten van de afgebroken runs: {_euro(cost_eur)}.",
        "Ik claim niets meer tot een mens `schakelaar/pauze-alles` weghaalt. Dat label kan ik "
        "zelf niet verwijderen; dat is met opzet zo gebouwd.",
    ])


def native_fallback_comment(agent_name: str, since: datetime, run_id: str) -> str:
    """De letterlijke terugvalcomment uit agent-roster 4."""
    display = agent_name.capitalize() if agent_name.islower() else agent_name
    return "\n\n".join([
        f"De tweede reviewer ({display}) was niet beschikbaar: sessie stond op awaitingInput "
        f"sinds {local_time(since)}. Ik val terug op Reviewer 1 (Fable 5.1). Dit is geen "
        "volwaardige dubbele review.",
        f"— Raderwerk · Spil (dispatcher) · run {run_id}",
    ])


def confirmation_comment(*, run_id: str, when: datetime, actor_name: str, decided_at: datetime,
                         source: str, source_id: str, outcome: str, next_state: str) -> str:
    """Spec 7.5 stap 4: wie is er gelezen, wanneer, en op welke registratie."""
    channel = "comment" if source == "comment" else "labelwissel"
    return "\n\n".join([
        signature("Spil (dispatcher)", "geen model", run_id, when),
        f"Poort {outcome}: gelezen van {actor_name or 'onbekend'} op {local_time(decided_at)}, "
        f"bron {channel} `{source_id}`.",
        f"**Volgende status** {next_state}",
    ])


def rejection_comment(*, run_id: str, when: datetime, reason: str, actor_name: str,
                      back_to: str, attempt: int) -> str:
    """Spec 7.6 stap 5: de reden komt letterlijk terug als opdracht, geen nieuw issue."""
    return "\n\n".join([
        signature("Spil (dispatcher)", "geen model", run_id, when),
        f"Poort afgekeurd door {actor_name or 'een mens'}. Dit is afkeuring {attempt} op deze "
        "poort. De reden staat hieronder letterlijk en is de opdracht voor de volgende ronde.",
        "**Reden**\n" + "\n".join(f"> {line}" for line in reason.strip().splitlines()),
        f"**Volgende status** {back_to}",
    ])


def stuck_comment(*, run_id: str, when: datetime, first_reason: str, second_reason: str) -> str:
    """Spec 7.6 stap 7: na de tweede afkeuring komt er geen derde poging."""
    return "\n\n".join([
        signature("Spil (dispatcher)", "geen model", run_id, when),
        "Deze poort is nu twee keer afgekeurd. Ik stop met dit issue; er komt geen derde poging.",
        "**Wat er twee keer misging**\n"
        f"1. {first_reason.strip() or 'geen reden vastgelegd'}\n"
        f"2. {second_reason.strip() or 'geen reden vastgelegd'}",
        "**Jouw drie keuzes**\n"
        "1. de opdracht herschrijven en het issue terugzetten\n"
        "2. naar een ander model routeren (label `agent/*`)\n"
        "3. het issue annuleren",
    ])


def unconfirmed_comment(*, run_id: str, when: datetime, refusal: str, source: Optional[str],
                        source_id: Optional[str], actor_name: Optional[str]) -> str:
    """Wat er precies gezien is en welke van de vijf voorwaarden faalde."""
    seen = [f"- kanaal: {source or 'onbekend'}"]
    if source_id:
        seen.append(f"- registratie: `{source_id}`")
    seen.append(f"- gelezen actor: {actor_name or 'niet vast te stellen'}")
    return "\n\n".join([
        signature("Spil (dispatcher)", "geen model", run_id, when),
        "Ik heb hier een poortsignaal gezien dat ik niet mag vertrouwen, dus ik doe niets.",
        "**Wat ik zag**\n" + "\n".join(seen),
        f"**Waarom ik het weiger** {refusal}",
        "Dit issue staat nu op `run/onbevestigd` en `schakelaar/mens-vereist`. Een mens moet dit "
        "vrijgeven; ik pak het uit mezelf niet meer op.",
    ])
