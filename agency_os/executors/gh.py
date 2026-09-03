"""De `gh`-CLI: een PR openen, terugvinden en uitlezen.

Zie docs/architecture.md sectie 3.6 en 10.1. In deze module bestaat met opzet
géén merge-functie: mergen is een onomkeerbare handeling en blijft mensenwerk
(AGENTS.md, "Verboden handelingen"). Dat is geen afspraak in een prompt maar een
ontbrekende tak in de code, met een test die ernaar kijkt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional, Sequence

from agency_os.executors.base import ExecutorConfig
from agency_os.executors.process import ProcessResult, run_process

__all__ = [
    "PR_FIELDS",
    "PullRequest",
    "find_pr_for_branch",
    "open_pr",
    "pr_diff",
    "read_pr",
]

#: Eén veldenlijst voor beide leesroutes, zodat `PullRequest` altijd compleet is.
PR_FIELDS = "number,url,state,isDraft,mergedAt,mergedBy,headRefOid,statusCheckRollup"

_GH_TIMEOUT_S = 120
_FAILED = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
_PENDING = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "EXPECTED"}


@dataclass(frozen=True)
class PullRequest:
    """Wat Spil van een PR moet weten om een poort te kunnen verantwoorden."""

    repo: str
    number: int
    url: str
    state: str  # OPEN | CLOSED | MERGED | dry-run
    is_draft: bool
    merged: bool
    merged_by_login: Optional[str]
    merged_by_is_bot: Optional[bool]
    head_sha: str
    checks_conclusion: Optional[str]  # success | failure | pending | None


def _gh(cfg: ExecutorConfig, args: Sequence[str]) -> ProcessResult:
    return run_process([cfg.gh_bin, *args], timeout_s=_GH_TIMEOUT_S)


def _is_bot(actor: Optional[dict]) -> Optional[bool]:
    """GitHub meldt een bot op drie manieren; alle drie tellen."""
    if not actor:
        return None
    login = str(actor.get("login") or "")
    return bool(actor.get("is_bot")) or actor.get("type") == "Bot" or login.endswith("[bot]")


def _checks(nodes: object) -> Optional[str]:
    """Vat de checkruns samen tot success / failure / pending, of None zonder checks."""
    if not isinstance(nodes, list) or not nodes:
        return None
    states = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        state = str(node.get("conclusion") or node.get("state") or node.get("status") or "").upper()
        states.add(state)
    if states & _FAILED:
        return "failure"
    if states & _PENDING or "" in states:
        return "pending"
    return "success"


def _parse_pr(repo: str, node: dict) -> PullRequest:
    merged_by = node.get("mergedBy") if isinstance(node.get("mergedBy"), dict) else None
    state = str(node.get("state") or "").upper()
    return PullRequest(
        repo=repo,
        number=int(node.get("number") or 0),
        url=str(node.get("url") or ""),
        state=state,
        is_draft=bool(node.get("isDraft")),
        merged=bool(node.get("mergedAt")) or state == "MERGED",
        merged_by_login=str(merged_by.get("login")) if merged_by else None,
        merged_by_is_bot=_is_bot(merged_by),
        head_sha=str(node.get("headRefOid") or ""),
        checks_conclusion=_checks(node.get("statusCheckRollup")),
    )


def find_pr_for_branch(cfg: ExecutorConfig, repo: str, branch: str) -> Optional[PullRequest]:
    """De PR die al bij deze branch hoort — de idempotentieproef vóór `open_pr`."""
    result = _gh(
        cfg,
        ["pr", "list", "--repo", repo, "--head", branch, "--state", "all",
         "--limit", "10", "--json", PR_FIELDS],
    )
    if not result.ok:
        return None
    try:
        nodes = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    prs = [_parse_pr(repo, node) for node in nodes if isinstance(node, dict)]
    if not prs:
        return None
    return next((pr for pr in prs if pr.state == "OPEN"), prs[0])


def open_pr(
    cfg: ExecutorConfig, repo: str, branch: str, base: str, title: str, body: str
) -> PullRequest:
    """Open een PR, of geef de bestaande terug. Een herstart opent er nooit een tweede."""
    existing = find_pr_for_branch(cfg, repo, branch)
    if existing is not None:
        return existing
    if cfg.dry_run:
        return PullRequest(repo, 0, "", "dry-run", False, False, None, None, "", None)

    result = _gh(
        cfg,
        ["pr", "create", "--repo", repo, "--head", branch, "--base", base,
         "--title", title, "--body", body],
    ).check()
    url = next(
        (line.strip() for line in reversed(result.stdout.splitlines()) if "/pull/" in line), ""
    )
    number = int(url.rsplit("/", 1)[-1]) if url.rsplit("/", 1)[-1].isdigit() else 0
    if number:
        return read_pr(cfg, repo, number)
    return PullRequest(repo, number, url, "OPEN", False, False, None, None, "", None)


def read_pr(cfg: ExecutorConfig, repo: str, number: int) -> PullRequest:
    """Lees één PR. Nodig voor de merge-verificatie van sectie 7.5: is hij door een mens gemerged?"""
    result = _gh(cfg, ["pr", "view", str(number), "--repo", repo, "--json", PR_FIELDS]).check()
    return _parse_pr(repo, json.loads(result.stdout or "{}"))


def pr_diff(cfg: ExecutorConfig, repo: str, number: int, *, max_bytes: int = 400_000) -> str:
    """De diff als tekst, afgekapt op `max_bytes` met een zichtbare afkapregel."""
    result = _gh(cfg, ["pr", "diff", str(number), "--repo", repo]).check()
    diff = result.stdout
    if len(diff.encode("utf-8")) <= max_bytes:
        return diff
    clipped = diff.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
    return f"{clipped}\n\n[diff afgekapt op {max_bytes} bytes]\n"
