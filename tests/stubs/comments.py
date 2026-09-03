"""Stand-in voor `agency_os.linear.comments` (onderdeel A), contract 3.4.

Genoeg vorm om te kunnen toetsen dat de handtekening er staat en dat de
poortkaart de tokens noemt zonder ze op regel 1 te zetten.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence


def signature(role_title: str, model_display: str, run_id: str, when: datetime) -> str:
    return f"**{role_title} · {model_display} · run {run_id} · {when:%Y-%m-%d %H:%M}**"


def claim_comment(run_id: str, when: datetime) -> str:
    return f"**Spil** claim {run_id} op {when:%Y-%m-%d %H:%M}"


def run_comment(*, role_title, model_display, run, body_md, evidence, dod, next_state) -> str:
    lines = [signature(role_title, model_display, run.run_id, run.gestart), "", body_md, "", "**Bewijs**"]
    lines += [f"- {a.label or a.type}: {a.url}" for a in evidence] or ["- geen"]
    lines += ["", f"**Definition of Done** {dod}", "", f"**Volgende status** {next_state}"]
    return "\n".join(lines)


def gate_card(*, gate_no, issue, what, evidence, criteria, reviewers, disagreement, risk,
              cost_so_far, high_risk, run_id, duration_s, cost_eur) -> str:
    token = "AKKOORD RISICO-GEZIEN" if high_risk else "AKKOORD"
    return "\n".join([
        f"**Poortkaart {gate_no}** — {issue.identifier}",
        "", what, "",
        "**Bewijs**", *[f"- {a.label or a.type}: {a.url}" for a in evidence],
        "", "**Criteria**", criteria,
        "", f"**Reviewers** {reviewers}", f"**Oneens** {disagreement}", f"**Risico** {risk}",
        f"**Kosten tot nu** {cost_so_far} (deze run € {cost_eur:.2f}, {duration_s:.0f}s, run {run_id})",
        "", f"Antwoord met {token} of met AFGEKEURD: <reden> als eerste regel van je comment.",
    ])


def qa_report(*, model_display, run_id, when, verdict, tested, criteria_rows, suite_ran, suite_output,
              findings_rows, edge_cases, not_verified, regression_risk) -> str:
    return "\n".join([signature("QA", model_display, run_id, when), "", f"**Oordeel** {verdict}", tested])


def halt_comment(run_id: str, when: datetime, aborted: int, elapsed_s: float, cost_eur: float) -> str:
    return "\n".join([
        signature("Spil", "dispatcher", run_id, when), "",
        f"Noodstop. {aborted} runs afgebroken, {elapsed_s:.0f} seconden na de omslag, € {cost_eur:.2f} kosten.",
    ])


def native_fallback_comment(agent_name: str, since: datetime, run_id: str) -> str:
    return (f"De tweede reviewer ({agent_name}) was niet beschikbaar: sessie stond op awaitingInput sinds "
            f"{since:%Y-%m-%d %H:%M}. Ik val terug op Reviewer 1 (Fable 5.1). "
            "Dit is geen volwaardige dubbele review.")
