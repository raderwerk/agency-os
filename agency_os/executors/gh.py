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
    "ChecksSummary",
    "PagesSite",
    "PullRequest",
    "find_pr_for_branch",
    "open_pr",
    "pages_site",
    "pr_checks",
    "pr_diff",
    "read_pr",
]

#: Eén veldenlijst voor beide leesroutes, zodat `PullRequest` altijd compleet is.
PR_FIELDS = ("number,url,state,isDraft,mergedAt,mergedBy,headRefOid,headRefName,"
             "statusCheckRollup")

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
    head_branch: str = ""


@dataclass(frozen=True)
class ChecksSummary:
    """De samenvatting van `gh pr checks`, zoals QA hem in zijn prompt krijgt."""

    verdict: str  # geslaagd | mislukt | loopt nog | geen checks | niet op te halen
    total: int = 0
    passed: int = 0
    failed: int = 0
    pending: int = 0
    failing: tuple[str, ...] = ()

    def __str__(self) -> str:
        if not self.total:
            return self.verdict
        line = f"{self.verdict}, {self.passed} van {self.total} checks groen"
        if self.failing:
            line += f"; rood: {', '.join(self.failing)}"
        return line


@dataclass(frozen=True)
class PagesSite:
    """Een GitHub Pages-site en de branch die erop gepubliceerd wordt."""

    url: str
    branch: Optional[str]  # None: gepubliceerd via een workflow, niet via een branch


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
        head_branch=str(node.get("headRefName") or ""),
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
        return PullRequest(repo, 0, "", "dry-run", False, False, None, None, "", None, branch)

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
    return PullRequest(repo, number, url, "OPEN", False, False, None, None, "", None, branch)


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


#: Wat `gh pr checks --json bucket` teruggeeft, vertaald naar het Nederlands van
#: de comments. `skipping` en `cancel` zijn geen falen en geen slagen: ze zeggen
#: dat de check niet gedraaid heeft.
_BUCKET_VERDICT = {"fail": "mislukt", "pending": "loopt nog", "pass": "geslaagd"}


def pr_checks(cfg: ExecutorConfig, repo: str, number: int) -> ChecksSummary:
    """De CI-uitslag van één PR, samengevat.

    Geeft nooit een uitzondering en gebruikt `.check()` bewust niet: `gh pr
    checks` sluit met afloopcode 8 af als er nog iets loopt en met 1 als er iets
    rood staat, terwijl de json in beide gevallen gewoon op stdout staat. Een
    onleesbaar antwoord wordt "niet op te halen" -- QA hoort te weten dat de
    uitslag ontbreekt, niet te denken dat alles groen is.
    """
    result = _gh(cfg, ["pr", "checks", str(number), "--repo", repo,
                       "--json", "bucket,name,state"])
    try:
        nodes = json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        nodes = None
    if not isinstance(nodes, list):
        return ChecksSummary("niet op te halen")
    if not nodes:
        return ChecksSummary("geen checks")

    buckets = [str(node.get("bucket") or "").lower() for node in nodes if isinstance(node, dict)]
    failing = tuple(
        str(node.get("name") or "?")
        for node in nodes
        if isinstance(node, dict) and str(node.get("bucket") or "").lower() == "fail"
    )
    counted = {name: buckets.count(name) for name in ("pass", "fail", "pending")}
    verdict = next(
        (_BUCKET_VERDICT[name] for name in ("fail", "pending", "pass") if counted[name]),
        "geen checks",
    )
    return ChecksSummary(
        verdict=verdict,
        total=len(buckets),
        passed=counted["pass"],
        failed=counted["fail"],
        pending=counted["pending"],
        failing=failing,
    )


def pages_site(cfg: ExecutorConfig, repo: str) -> Optional[PagesSite]:
    """De GitHub Pages-site van deze repo, of None als er geen staat.

    404 is het normale antwoord: de meeste repo's van de werkplaats publiceren
    niets. Dat is informatie voor QA en geen fout.
    """
    result = _gh(cfg, ["api", f"repos/{repo}/pages"])
    if not result.ok:
        return None
    try:
        data = json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("message"):
        return None
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    url = str(data.get("html_url") or "") or f"https://raderwerk.github.io/{repo.split('/')[-1]}/"
    return PagesSite(url=url, branch=str(source.get("branch") or "") or None)
