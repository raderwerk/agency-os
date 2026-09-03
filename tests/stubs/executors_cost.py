"""Stand-in voor `agency_os.executors.cost` (onderdeel B), contract 3.6."""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from agency_os.executors.base import Usage


def normalise(usage: Usage, model_ledger: str, prices: Sequence) -> Usage:
    if usage.cost_usd or not usage.metered:
        return usage
    row = next((p for p in prices if p.model == model_ledger), None)
    if row is None:
        return replace(usage, source="unknown")
    usd = (usage.tokens_in * row.usd_in_per_mtok
           + usage.tokens_out * row.usd_out_per_mtok
           + usage.cache_read * row.usd_cache_read_per_mtok) / 1_000_000
    return replace(usage, cost_usd=usd)


def to_eur(usd: float, fx) -> float:
    return usd * fx.usd_eur


def budget_flag(total_eur_for_issue: float, threshold_eur: float = 10.0) -> bool:
    return total_eur_for_issue > threshold_eur
