"""Het bewijsblok voor de rollen die beoordelen in plaats van maken.

QA en de Reviewer moeten criteria afvinken die over artefacten gaan: staat er
een pull request, is de CI groen, wat staat er op de preview, en wat vond de
vorige beoordelaar. De Reviewer zoekt zijn PR nu zelf op met `find_pr_for_branch`
omdat hij op de codex-laan draait; QA draait op de Claude-laan, heeft geen `gh`
(die is met opzet uitgeschakeld, zie `claude_runner.DENIED_TOOLS`) en kreeg dat
alles nergens te zien. Hij kon de diff in zijn werkmap lezen en verder niets.

Dit bestand vult daarom de `extra_context`-haak van `prompts.build_prompt`. Het
schrijft niets en beslist niets: elke opzoeking is leesbaar, en elke opzoeking
die mislukt wordt een zin in de prompt in plaats van een uitzondering. Een run
mag niet omvallen omdat `gh` even niet kon inloggen.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence

from agency_os.executors.gh import ChecksSummary, find_pr_for_branch, pages_site, pr_checks

__all__ = ["EVIDENCE_HEADING", "for_role", "evidence_block"]

EVIDENCE_HEADING = "Bewijsmateriaal"

#: De rollen waarvan het oordeel in dit blok hoort, in de volgorde waarin ze
#: langskomen. De handtekening van spec 8.3 begint met de roltitel, dus daar
#: zijn ze aan te herkennen zonder de comment verder te lezen.
VERDICT_ROLES = ("Reviewer 1", "Reviewer 2", "QA · rookproef", "QA")

_SIGNATURE = re.compile(r"^\*\*(?P<role>[^·*]+?)\s*·", re.MULTILINE)


def for_role(cfg: Any, role: Any, issue: Any, *, branch: str,
             discussion: Sequence[Any] = ()) -> Mapping[str, str]:
    """`{kop: blok}` voor `build_prompt`, of `{}` voor een rol die niets beoordeelt."""
    if not getattr(role, "needs_evidence", False):
        return {}
    return {EVIDENCE_HEADING: evidence_block(cfg, issue, branch=branch, discussion=discussion)}


def evidence_block(cfg: Any, issue: Any, *, branch: str,
                   discussion: Sequence[Any] = ()) -> str:
    """De artefacten van dit issue, opgezocht door de Spil en niet door de rol."""
    verdicts = _verdicts(discussion)
    pull_request = _pull_request(cfg, issue, branch)
    if pull_request is None:
        return "\n\n".join([_no_pull_request(issue, branch), *verdicts])

    lines = [
        f"- Pull request: PR #{pull_request.number} — {pull_request.url} "
        f"({_pr_state(pull_request)})",
        f"- CI: {_checks(cfg, issue, pull_request)}",
        f"- Preview: {_preview(cfg, issue, pull_request)}",
        f"- Branch: `{pull_request.head_branch or branch}`, "
        f"HEAD {_short(pull_request.head_sha)}",
    ]
    intro = ("Dit heeft de Spil voor je opgezocht; je hebt zelf geen `gh` en hebt hem ook niet "
             "nodig. Vink een criterium alleen af als het bewijs hier of in je werkmap staat.")
    return "\n\n".join(["\n".join([intro, "", *lines]), *verdicts])


# --------------------------------------------------------------------------
# de losse regels
# --------------------------------------------------------------------------


def _pull_request(cfg: Any, issue: Any, branch: str) -> Optional[Any]:
    """De PR bij deze branch, of None -- ook als `gh` zelf niet meewerkte."""
    repo = getattr(issue, "repo", None)
    if not repo or not branch:
        return None
    try:
        return find_pr_for_branch(cfg, repo, branch)
    except Exception:  # noqa: BLE001 - een kapotte gh mag geen run kosten
        return None


def _pr_state(pull_request: Any) -> str:
    if pull_request.merged:
        return "gemerged"
    if pull_request.is_draft:
        return "concept, nog niet klaar voor beoordeling"
    return (pull_request.state or "?").lower()


def _checks(cfg: Any, issue: Any, pull_request: Any) -> str:
    try:
        summary = pr_checks(cfg, issue.repo, pull_request.number)
    except Exception:  # noqa: BLE001
        summary = ChecksSummary("niet op te halen")
    if summary.verdict == "niet op te halen":
        return ("niet op te halen. Meld elk criterium dat op een groene CI leunt als "
                "*niet te verifiëren*.")
    return str(summary)


def _preview(cfg: Any, issue: Any, pull_request: Any) -> str:
    """De preview-URL, met erbij of deze branch er al op staat."""
    try:
        site = pages_site(cfg, issue.repo)
    except Exception:  # noqa: BLE001
        site = None
    if site is None:
        return "deze repo publiceert geen GitHub Pages; er is geen preview om te openen."
    branch = pull_request.head_branch or ""
    if site.branch and branch and site.branch == branch:
        return f"{site.url} (deze branch staat erop)"
    published = f"`{site.branch}`" if site.branch else "de gepubliceerde branch"
    return (f"{site.url} — pas ná merge. GitHub Pages publiceert {published}, en het werk van "
            "deze branch staat daar dus nog niet op.")


def _short(sha: str) -> str:
    return sha[:12] if sha else "onbekend"


def _no_pull_request(issue: Any, branch: str) -> str:
    """Waarom het blok leeg is. Stilte zou hier voor "alles in orde" doorgaan."""
    where = f"branch `{branch}`" if branch else "dit issue"
    repo = getattr(issue, "repo", None)
    reason = f"in {repo}" if repo else "want dit issue noemt geen repo"
    return (
        f"Er is geen pull request voor {where} {reason}. Daarmee is er ook geen CI-uitslag, geen "
        "HEAD-sha en geen preview. Beoordeel wat er in je werkmap staat, en meld elk "
        "acceptatiecriterium dat een pull request, een groene CI of een preview nodig heeft als "
        "*niet te verifiëren*. Verzin geen link."
    )


def _verdicts(discussion: Sequence[Any]) -> list[str]:
    """Wie er al geoordeeld heeft, en wanneer. Niet wát ze zeiden.

    De tekst zelf staat al voluit in het blok "Discussie op het issue"; die hier
    herhalen zou het promptbudget twee keer opeten en de rol twee versies van
    hetzelfde oordeel geven. Dit is de wegwijzer erheen, zodat een oordeel dat
    ver naar boven weggezakt is niet over het hoofd gezien wordt.
    """
    seen: dict[str, Any] = {}
    for comment in sorted(discussion, key=lambda c: c.created_at):
        found = _SIGNATURE.search((comment.body or "").strip())
        role = found.group("role").strip() if found else ""
        if role in VERDICT_ROLES:
            seen[role] = comment
    if not seen:
        return []
    lines = [
        f"- {role}: {comment.created_at:%Y-%m-%d %H:%M} UTC"
        for role, comment in sorted(seen.items(), key=lambda item: item[1].created_at)
    ]
    return ["\n".join([
        "Eerdere oordelen op dit issue, met de nieuwste per rol:",
        "",
        *lines,
        "",
        'De volledige tekst staat in het blok "Discussie op het issue" hierboven; hij wordt hier '
        "niet herhaald.",
    ])]
