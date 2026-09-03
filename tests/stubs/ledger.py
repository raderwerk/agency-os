"""Stand-in voor `agency_os.linear.ledger` (onderdeel A), contract 3.4."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping, Optional, Sequence


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


def record_run(store, run) -> None:
    store.add_run(run)


def render_tail_block(run) -> str:
    return "\n".join([
        "```yaml", f"run: {run.run_id}", f"rol: {run.rol}", f"model: {run.model}",
        f"issue: {run.issue_identifier}", f"uitkomst: {run.uitkomst}",
        f"volgende_status: {run.volgende_status}", f"gemeten: {'true' if run.metered else 'false'}", "```",
    ])


def parse_tail_block(comment_body: str):
    return None


def rollup(store, day: date) -> DayRollup:
    rows = store.runs_on(day)
    usd = sum(row["kosten_usd"] for row in rows)
    eur = sum(row["kosten_eur"] for row in rows)
    by_role: dict[str, float] = {}
    for row in rows:
        by_role[row["rol"]] = by_role.get(row["rol"], 0.0) + row["kosten_eur"]
    return DayRollup(
        day=day, runs=len(rows), issues=len({row["issue_id"] for row in rows}), usd=usd, eur=eur,
        by_role=by_role, by_klant={}, gates_passed=0, gates_rejected=0, median_gate_wait_s=None,
        supervision_minutes=0.0, first_pass_ok=(0, 0), issue_count=0, loops=0,
    )


def render_markdown(store, *, since: date, until: date, prices: Sequence[PriceRow], fx: FxRate) -> str:
    return (f"# Kostenboek {since} tot {until}\n\n"
            f"Koers {fx.usd_eur} ({fx.source}, {fx.on}). Schatting op lijstprijs, geen factuurdata. "
            "De native lanes vallen buiten dit boek; de eenheidseconomie is dus onvolledig.\n")
