"""Verbruik omrekenen naar geld, zonder te doen alsof onbekend nul is.

Zie docs/architecture.md sectie 12. Twee regels die het Kostenboek eerlijk
houden: een bedrag dat de executor zelf meldde wint van elke schatting, en wat
niet te meten valt krijgt `metered=False` in plaats van een gemiddelde.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Optional, Sequence

from agency_os.executors.base import Usage

if TYPE_CHECKING:  # pragma: no cover - A levert de prijstabel in ledger.py
    from agency_os.linear.ledger import FxRate, PriceRow

__all__ = ["BUDGET_THRESHOLD_EUR", "budget_flag", "normalise", "to_eur"]

#: Boven dit bedrag krijgt een issue `budget-let-op`. Informatie, geen rem (spec 8.6).
BUDGET_THRESHOLD_EUR = 10.0

_PER_MTOK = 1_000_000.0


def _price_for(model_ledger: str, prices: Sequence["PriceRow"]) -> Optional["PriceRow"]:
    wanted = (model_ledger or "").strip().lower()
    for row in prices:
        if row.model.strip().lower() == wanted:
            return row
    return None


def normalise(usage: Usage, model_ledger: str, prices: Sequence["PriceRow"]) -> Usage:
    """Vul `cost_usd` aan met de lijstprijs, of markeer de run als ongemeten.

    Volgorde: een gemeld bedrag blijft staan; anders wordt het uit de tokens en
    de prijstabel berekend; ontbreekt de prijsregel of is er niets gemeten, dan
    wordt `metered=False` en blijft het bedrag nul.
    """
    if not usage.metered:
        return replace(usage, cost_usd=0.0)
    if usage.cost_usd > 0:
        return usage

    tokens = usage.tokens_in + usage.tokens_out + usage.cache_read
    row = _price_for(model_ledger, prices)
    if row is None or tokens == 0:
        return replace(usage, cost_usd=0.0, metered=False)

    cost_usd = (
        usage.tokens_in * row.usd_in_per_mtok
        + usage.tokens_out * row.usd_out_per_mtok
        + usage.cache_read * row.usd_cache_read_per_mtok
    ) / _PER_MTOK
    return replace(usage, cost_usd=cost_usd)


def to_eur(usd: float, fx: "FxRate") -> float:
    """Koers uit de configuratie, nooit een vaste waarde in de code (WV-161)."""
    return usd * fx.usd_eur


def budget_flag(total_eur_for_issue: float, threshold_eur: float = BUDGET_THRESHOLD_EUR) -> bool:
    """True zodra het issue over de drempel heen is en `budget-let-op` verdient."""
    return total_eur_for_issue >= threshold_eur
