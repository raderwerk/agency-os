"""Het kostenboek: één regel per run, ook als de run mislukte.

Een kostenboek dat alleen successen bevat is een marketingdocument, dus
`record_run` wordt onvoorwaardelijk aangeroepen.

Het staartblok (spec 8.3) is machineleesbaar en wordt hier zowel geschreven als
teruggelezen; de heen-en-weer-test is verplicht. Eén sleutel meer dan de spec:
`gemeten`, die per regel zegt of het getal echt gemeten is. De native lanes
(Codex, Cursor) rekenen binnen hun eigen abonnement af en zijn dus onmeetbaar;
dat hoort per regel te staan en niet alleen in een voetnoot.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Mapping, Optional, Sequence

from .models import Artifact, RunRecord
from .store import Store, iso, parse_iso

__all__ = [
    "PriceRow", "FxRate", "DayRollup", "record_run", "render_tail_block",
    "parse_tail_block", "rollup", "render_markdown",
]

INCOMPLETENESS = (
    "Het tokenverbruik van Codex en Cursor loopt buiten dit kostenboek om; zolang die twee "
    "lanes native zijn, is de unit economics structureel incompleet."
)
ESTIMATE_NOTICE = (
    "Dit zijn cliëntzijdige schattingen op lijstprijs, geen factuurgegevens."
)

_FENCE = re.compile(r"^\s*(```|~~~)\s*([A-Za-z0-9_-]*)\s*$")


@dataclass(frozen=True)
class PriceRow:
    model: str
    usd_in_per_mtok: float
    usd_out_per_mtok: float
    usd_cache_read_per_mtok: float


@dataclass(frozen=True)
class FxRate:
    usd_eur: float
    source: str
    on: date


@dataclass(frozen=True)
class DayRollup:
    day: date
    runs: int
    issues: int
    usd: float
    eur: float
    by_role: Mapping[str, float]
    by_klant: Mapping[str, float]
    gates_passed: int
    gates_rejected: int
    median_gate_wait_s: Optional[float]
    supervision_minutes: float
    first_pass_ok: tuple[int, int]
    issue_count: int
    loops: int


def record_run(store: Store, run: RunRecord) -> None:
    """Schrijft de runregel weg. Altijd, ook bij `mislukt` en `afgebroken`."""
    store.insert_run(run)


# ---------------- staartblok ----------------

def _num(value: float) -> str:
    """Zo kort mogelijk, maar heen-en-weer gelijk. Afronden op centen doet de render."""
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def render_tail_block(run: RunRecord) -> str:
    """Het yaml-staartblok van spec 8.3, plus `gemeten`."""
    lines = [
        "```yaml",
        f"run: {run.run_id}",
        f"rol: {run.rol}",
        f"model: {run.model}",
        f"issue: {run.issue_identifier}",
        f"gestart: {iso(run.gestart)}",
        f"geeindigd: {iso(run.geeindigd) or ''}",
        f"duur_s: {_num(run.duur_s)}",
        f"kosten_usd: {_num(run.kosten_usd)}",
        f"kosten_eur: {_num(run.kosten_eur)}",
        f"tokens_in: {run.tokens_in}",
        f"tokens_uit: {run.tokens_uit}",
        f"cache_lees: {run.cache_lees}",
        f"beurten: {run.beurten}",
        f"dod: {run.dod}",
        f"uitkomst: {run.uitkomst}",
        f"volgende_status: {run.volgende_status or ''}",
        f"gemeten: {'true' if run.metered else 'false'}",
    ]
    if run.artefacten:
        lines.append("artefacten:")
        for artifact in run.artefacten:
            lines.append(f"  - type: {artifact.type}")
            lines.append(f"    url: {artifact.url}")
            if artifact.label:
                lines.append(f"    label: {artifact.label}")
    else:
        lines.append("artefacten: []")
    lines.append("```")
    return "\n".join(lines)


def _fenced_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    fence: Optional[str] = None
    body: list[str] = []
    for line in text.splitlines():
        match = _FENCE.match(line)
        if fence is None:
            if match:
                fence, body = match.group(1), []
            continue
        if match and match.group(1) == fence:
            blocks.append("\n".join(body))
            fence = None
            continue
        body.append(line)
    return blocks


def parse_tail_block(comment_body: str) -> Optional[RunRecord]:
    """Leest het staartblok terug uit een comment. None als er geen in staat.

    Vier velden staan niet in het blok en worden afgeleid in plaats van geraden:
    `team_key` uit de identifier, `pr_url` uit de artefacten, en `issue_id` /
    `executor` blijven leeg omdat een comment ze niet draagt.
    """
    if not comment_body:
        return None
    for block in reversed(_fenced_blocks(comment_body)):
        pairs, artifacts = _parse_tail_yaml(block)
        if "run" not in pairs:
            continue
        identifier = pairs.get("issue", "")
        pr_url = next((a.url for a in artifacts if a.type == "pr"), None)
        return RunRecord(
            run_id=pairs.get("run", ""),
            issue_id="",
            issue_identifier=identifier,
            team_key=identifier.split("-")[0] if "-" in identifier else "",
            rol=pairs.get("rol", ""),
            model=pairs.get("model", ""),
            executor="",
            klant=None,
            dienst=None,
            gestart=parse_iso(pairs.get("gestart")) or datetime.now(timezone.utc),
            geeindigd=parse_iso(pairs.get("geeindigd")),
            duur_s=float(pairs.get("duur_s") or 0),
            beurten=int(float(pairs.get("beurten") or 0)),
            tokens_in=int(float(pairs.get("tokens_in") or 0)),
            tokens_uit=int(float(pairs.get("tokens_uit") or 0)),
            cache_lees=int(float(pairs.get("cache_lees") or 0)),
            kosten_usd=float(pairs.get("kosten_usd") or 0),
            kosten_eur=float(pairs.get("kosten_eur") or 0),
            dod=pairs.get("dod") or "-",
            uitkomst=pairs.get("uitkomst") or "mislukt",
            volgende_status=pairs.get("volgende_status") or None,
            pr_url=pr_url,
            artefacten=tuple(artifacts),
            metered=(pairs.get("gemeten", "true").lower() != "false"),
        )
    return None


def _parse_tail_yaml(block: str) -> tuple[dict[str, str], list[Artifact]]:
    pairs: dict[str, str] = {}
    artifacts: list[Artifact] = []
    current: dict[str, str] = {}
    in_list = False
    for line in block.splitlines():
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped == "artefacten:" or stripped.startswith("artefacten:"):
            in_list = stripped.endswith(":")
            if not in_list:
                pairs["artefacten"] = stripped.split(":", 1)[1].strip()
            continue
        if in_list and stripped.startswith("- "):
            if current:
                artifacts.append(_to_artifact(current))
            key, _, value = stripped[2:].partition(":")
            current = {key.strip(): value.strip()}
            continue
        if in_list and line.startswith("    ") and ":" in stripped:
            key, _, value = stripped.partition(":")
            current[key.strip()] = value.strip()
            continue
        if ":" in stripped:
            in_list = False
            if current:
                artifacts.append(_to_artifact(current))
                current = {}
            key, _, value = stripped.partition(":")
            pairs[key.strip()] = value.strip()
    if current:
        artifacts.append(_to_artifact(current))
    return pairs, artifacts


def _to_artifact(raw: Mapping[str, str]) -> Artifact:
    return Artifact(type=raw.get("type", ""), url=raw.get("url", ""), label=raw.get("label", ""))


# ---------------- roll-up ----------------

def rollup(store: Store, day: date) -> DayRollup:
    """De dagafsluiting van spec hoofdstuk 11, uit sqlite in plaats van uit proza."""
    runs = store.runs_on(day)
    by_role: dict[str, float] = {}
    by_klant: dict[str, float] = {}
    for run in runs:
        by_role[run.rol] = by_role.get(run.rol, 0.0) + run.kosten_eur
        by_klant[run.klant or "overig"] = by_klant.get(run.klant or "overig", 0.0) + run.kosten_eur

    events = store.gate_events_between(day, day)
    applied = [e for e in events if e["applied_at"] and e["valid"]]
    waits: list[float] = []
    for event in applied:
        card_at, decided_at = parse_iso(event["card_at"]), parse_iso(event["decided_at"])
        if card_at and decided_at and decided_at > card_at:
            waits.append((decided_at - card_at).total_seconds())

    finished = [r for r in runs if r.volgende_status == "Klaar" and r.uitkomst == "klaar"]
    clean = [r for r in finished if _issue_was_clean(store, r.issue_id, day)]
    return DayRollup(
        day=day,
        runs=len(runs),
        issues=len({r.issue_id for r in runs}),
        usd=sum(r.kosten_usd for r in runs),
        eur=sum(r.kosten_eur for r in runs),
        by_role=by_role,
        by_klant=by_klant,
        gates_passed=sum(1 for e in applied if e["outcome"] == "akkoord"),
        gates_rejected=sum(1 for e in applied if e["outcome"] == "afgekeurd"),
        median_gate_wait_s=(statistics.median(waits) if waits else None),
        supervision_minutes=sum(waits) / 60.0,
        first_pass_ok=(len(clean), len(finished)),
        issue_count=int(store.get_meta("issue_count", "0") or 0),
        loops=store.loops_on(day),
    )


def _issue_was_clean(store: Store, issue_id: str, day: date) -> bool:
    """Klaar zonder afkeuring en zonder herstelrun: dat telt als eerste-keer-goed."""
    rejected = store.conn.execute(
        "SELECT COUNT(*) AS n FROM gate_events WHERE issue_id = ? AND outcome = 'afgekeurd' "
        "AND valid = 1", (issue_id,)).fetchone()
    repairs = store.conn.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE issue_id = ? "
        "AND uitkomst IN ('mislukt','afgebroken')",
        (issue_id,)).fetchone()
    return int(rejected["n"]) == 0 and int(repairs["n"]) == 0


# ---------------- markdown ----------------

def _pct(part: float, whole: float) -> str:
    return f"{(100.0 * part / whole):.0f}%" if whole else "0%"


def _eur(amount: float) -> str:
    return ("€ %.2f" % amount).replace(".", ",")


def render_markdown(store: Store, *, since: date, until: date, prices: Sequence[PriceRow],
                    fx: FxRate) -> str:
    """De drie secties van D05: koersen, runregels, dagafsluitingen."""
    lines: list[str] = [f"# Kostenboek {since.isoformat()} t/m {until.isoformat()}", ""]

    lines += ["## 1. Koersen en aannames", "",
              f"Wisselkoers USD -> EUR: {fx.usd_eur} ({fx.source}, {fx.on.isoformat()}).", "",
              "| Model | $ in / Mtok | $ uit / Mtok | $ cache-lees / Mtok |",
              "|---|---|---|---|"]
    for price in prices:
        lines.append(f"| {price.model} | {price.usd_in_per_mtok} | {price.usd_out_per_mtok} | "
                     f"{price.usd_cache_read_per_mtok} |")
    lines += ["", ESTIMATE_NOTICE, "", INCOMPLETENESS, ""]

    runs = store.runs_between(since, until)
    lines += ["## 2. Runregels", "",
              "| Datum | Issue | Rol | Model | Beurten | Tokens in | Tokens uit | Cache-lees | "
              "USD | EUR | Duur | Uitkomst | Gemeten |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for run in runs:
        lines.append(
            f"| {run.gestart.date().isoformat()} | {run.issue_identifier} | {run.rol} | "
            f"{run.model} | {run.beurten} | {run.tokens_in} | {run.tokens_uit} | "
            f"{run.cache_lees} | {run.kosten_usd:.2f} | {run.kosten_eur:.2f} | "
            f"{run.duur_s:.0f}s | {run.uitkomst} | {'ja' if run.metered else 'nee'} |")
    if not runs:
        lines.append("|" + " — |" * 13)
    lines.append("")

    lines += ["## 3. Dagafsluiting", ""]
    for day in sorted({run.gestart.date() for run in runs}) or [until]:
        day_rollup = rollup(store, day)
        lines += ["```", _render_close(day_rollup), "```", ""]
    return "\n".join(lines)


def _render_close(day: DayRollup) -> str:
    """Het blok uit spec hoofdstuk 11, met supervisie en eerste-keer-goed vooraan in belang."""
    roles = " · ".join(f"{role} {_pct(amount, day.eur)}"
                       for role, amount in sorted(day.by_role.items(), key=lambda kv: -kv[1]))
    klanten = " · ".join(f"{klant} {_pct(amount, day.eur)}"
                         for klant, amount in sorted(day.by_klant.items(), key=lambda kv: -kv[1]))
    wait = (f"{day.median_gate_wait_s / 60:.0f} min" if day.median_gate_wait_s is not None
            else "geen")
    ok, total = day.first_pass_ok
    return "\n".join([
        f"{day.day.isoformat()} · {day.runs} runs · {day.issues} issues aangeraakt",
        f"kosten: ${day.usd:.2f} / {_eur(day.eur)}",
        f"per rol: {roles or 'geen'}",
        f"per klant: {klanten or 'geen'}",
        f"poorten: {day.gates_passed} gepasseerd, {day.gates_rejected} afgekeurd, "
        f"mediane wachttijd {wait}",
        f"supervisie: {day.supervision_minutes:.0f} minuten menselijke tijd over "
        f"{day.gates_passed + day.gates_rejected} poortmomenten",
        f"eerste-keer-goed: {ok} van {total} ({_pct(ok, total)})",
        f"issueteller: {day.issue_count} / 250",
        f"lussen: {'geen' if not day.loops else day.loops}",
    ])
