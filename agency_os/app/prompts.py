"""De runprompt: skelet + rolblok + issue + repo-instructies + uitvoercontract.

Samengesteld door C, letterlijk doorgegeven aan B. De uitvoerder verandert er
niets aan; dat is de enige manier om achteraf te kunnen zeggen wat een model
precies te lezen kreeg.

De volgorde is die van D03: eerst de onwrikbare regels, dan pas de rol en het
issue. Instructies uit het issue staan bewust ná de regels die ze niet mogen
overrulen (spec 7.8, weerbaarheid tegen instructie-injectie).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Optional

from agency_os.app.routing import ROLES_DIR, RoleSpec

SKELETON_FILE = ROLES_DIR / "_skelet.md"

OUTPUT_CONTRACT = """## Uitvoercontract

Je schrijft precies één comment. Die begint met de handtekening en eindigt met
een machineleesbaar staartblok. Daartussen: wat je deed in gewone zinnen, een
kopje **Bewijs** met links, de Definition of Done met per punt een bewijslink,
en de volgende status.

De handtekening is de eerste regel:

    **<Rol> · <model> · run <run-id> · <tijdstempel>**

Sluit je uitvoer af met precies één gemarkeerd blok, als laatste blok, met de
taalmarkering `json RUNRESULT`:

```json RUNRESULT
{
  "uitkomst": "klaar",
  "samenvatting": "Eén of twee zinnen Nederlands over wat er nu is.",
  "dod": "6/6",
  "vraag": null,
  "pr_url": "https://github.com/raderwerk/<repo>/pull/<n>",
  "bewijs": [
    {"type": "pr", "url": "https://github.com/raderwerk/<repo>/pull/<n>", "label": "PR #<n>"},
    {"type": "test", "url": "<link naar de testuitvoer>", "label": "suite groen"}
  ]
}
```

`uitkomst` is `klaar`, `vraag`, `mislukt` of `afgebroken`. Bij `vraag` vul je
`vraag` met één scherpe vraag en laat je de rest staan. `bewijs[].type` is `pr`,
`preview`, `document`, `screenshot` of `test`. Ontbreekt dit blok of is het niet
te lezen, dan telt de run als mislukt: er wordt niets uit je proza geraden.
"""


def build_prompt(
    cfg: Any,
    role: RoleSpec,
    issue: Any,
    *,
    run_id: str,
    branch: str = "",
    base_branch: str = "main",
    extra_context: Mapping[str, str] | None = None,
) -> str:
    """Stelt de volledige prompt samen voor één run van één rol op één issue."""
    extra = dict(extra_context or {})
    agents_md, agents_note = _agents_md(cfg, issue, override=extra.pop("agents_md", None))
    blocks = [
        _read(SKELETON_FILE),
        _read(role.prompt_path),
        _issue_block(issue, role=role, run_id=run_id),
        _criteria_block(issue.description or "", agents_md=agents_md),
        _repo_block(issue, agents_md, agents_note),
        _workspace_block(role, issue, branch=branch, base_branch=base_branch),
        OUTPUT_CONTRACT,
    ]
    blocks.extend(f"## {title}\n\n{body}" for title, body in extra.items())
    return "\n\n---\n\n".join(block.strip() for block in blocks if block and block.strip()) + "\n"


def acceptance_criteria(description: str) -> list[str]:
    """De regels onder '## Acceptatiecriteria', zonder opsommings- of vinktekens."""
    return _section_items(description, "acceptatiecriteria")


def dod_items(description: str) -> list[str]:
    """De regels onder '## Definition of Done'."""
    return _section_items(description, "definition of done")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _issue_block(issue: Any, *, role: RoleSpec, run_id: str) -> str:
    contract = getattr(issue, "contract", None)
    lines = [
        "## Issue",
        "",
        f"- Issue: {issue.identifier} — {issue.title}",
        f"- Link: {issue.url}",
        f"- Bord en status: {issue.team_key} / {issue.state_name}",
        f"- Labels: {', '.join(issue.labels) or 'geen'}",
        f"- Jouw rol: {role.title} (run {run_id})",
        f"- Volgende status bij uitkomst klaar: {role.done_state}",
    ]
    if contract is not None and getattr(contract, "raw", ""):
        lines += ["", "### Opdrachtcontract", "", "```yaml", contract.raw.strip(), "```"]
    lines += ["", "### Omschrijving", "", (issue.description or "_geen omschrijving_").strip()]
    return "\n".join(lines)


def _criteria_block(description: str, *, agents_md: str = "") -> str:
    """De criteria en de DoD, met de dienstlijn-DoD uit AGENTS.md als terugval.

    Een issue dat naar een sjabloon verwijst ("Volgt het sjabloon `Contentstuk`")
    heeft geen eigen DoD-lijst. Zonder de terugval zei dit blok dan "niet
    gevonden, vraag ernaar" terwijl de lijst twee schermen lager in het
    repo-blok van dezelfde prompt staat -- en regel 6 maakt van zo'n run een
    vraag in plaats van werk.
    """
    criteria = acceptance_criteria(description)
    dod = dod_items(description)
    dod_source = "het issue"
    if not dod and agents_md:
        dod = dod_items(agents_md)
        dod_source = "AGENTS.md van de repo"
    lines = ["## Acceptatiecriteria en Definition of Done", ""]
    if criteria:
        lines += ["Acceptatiecriteria:", ""] + [f"{i}. {item}" for i, item in enumerate(criteria, 1)] + [""]
    else:
        lines += ["Acceptatiecriteria: niet in het issue gevonden. Vraag ernaar in plaats van ze te verzinnen.", ""]
    if dod:
        lines += [f"Definition of Done (uit {dod_source}):", ""] + [f"- [ ] {item}" for item in dod]
    else:
        lines += ["Definition of Done: niet in het issue gevonden. Vraag ernaar."]
    lines += ["", f"Vink alleen af wat je met een link in dezelfde comment kunt bewijzen ({len(dod)} punten)."]
    return "\n".join(lines)


def _agents_md(cfg: Any, issue: Any, *, override: Optional[str]) -> tuple[str, str]:
    """De AGENTS.md van de doelrepo, plus waar hij vandaan komt.

    Geeft `("", "")` als het issue geen repo noemt. Ontbreekt het bestand, dan
    is de tekst leeg en zegt de notitie waar er gekeken is.
    """
    repo = getattr(issue, "repo", None)
    if override:
        return override, ""
    if not repo:
        return "", ""
    root = Path(getattr(cfg.executors, "repo_root", Path.home()))
    agents = root / repo.split("/")[-1] / "AGENTS.md"
    text = _read(agents)
    return (text, f"uit {agents.name}") if text else ("", str(agents))


def _repo_block(issue: Any, agents_md: str, note: str) -> str:
    """De repo-instructies, of de mededeling dat ze niet lokaal stonden."""
    repo = getattr(issue, "repo", None)
    if agents_md:
        heading = f"{repo or 'onbekend'}, {note}" if note else (repo or "onbekend")
        return f"## Repo-instructies ({heading})\n\n{agents_md}"
    if not repo:
        return ""
    return (
        f"## Repo-instructies ({repo})\n\n"
        f"Niet gevonden op {note}. Lees AGENTS.md in de worktree voordat je iets wijzigt."
    )


def _workspace_block(role: RoleSpec, issue: Any, *, branch: str, base_branch: str) -> str:
    """Waar het model draait en wie de branch pusht.

    Zonder dit blok weet een rol met een werkmap niet dat hij er al in staat, en
    vooral niet dát hij moet committen: de Spil pusht wat er gecommit is, en
    `has_commits_ahead` maakt van een werkmap vol ongecommitte wijzigingen een
    mislukte run zonder pull request. Dat is precies de eerste manier waarop een
    live run stukloopt, en het stond nergens in de prompt.
    """
    if not role.needs_worktree:
        return ""
    lines = [
        "## Werkmap",
        "",
        f"- Repo: {issue.repo or 'onbekend'}",
        f"- Basisbranch: {base_branch}",
    ]
    if branch:
        lines.append(f"- Branch: {branch}")
    lines += [
        "",
        "Je huidige map is de git-werkmap voor dit issue en staat al op die branch. "
        "Vertak niet, wissel niet van map en werk nergens anders.",
        "",
    ]
    if role.needs_pr:
        lines.append(
            "Commit je werk in deze werkmap, met Engelse commitberichten. De Spil pusht de "
            "branch en opent daarna de pull request; `git push` en `gh` zijn voor jou "
            "uitgeschakeld en je hebt ze niet nodig. Laat je wijzigingen ongecommit staan, "
            "dan is er niets om te pushen, komt er geen pull request en telt de run als "
            "mislukt."
        )
    else:
        lines.append(
            "Je beoordeelt hier alleen. Wijzig niets, commit niets en push niets: het werk "
            "dat je bekijkt staat al op deze branch."
        )
    return "\n".join(lines)


_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*$")
_ITEM = re.compile(r"^\s*(?:[-*+]\s+(?:\[[ xX]\]\s*)?|\d+[.)]\s+)(?P<text>.+?)\s*$")


def _section_items(description: str, heading: str) -> list[str]:
    """Opsommingsregels onder een kop, ongeacht kopniveau of hoofdletters.

    De kop mag een toelichting tussen haakjes dragen: AGENTS.md schrijft
    "## Definition of Done (dienstlijn content, sjabloon `Contentstuk`)" en dat
    is dezelfde sectie. Alleen die vorm telt mee, zodat "Definition of Done is
    hier niet van toepassing" geen sectie wordt.
    """
    items: list[str] = []
    inside = False
    for line in (description or "").splitlines():
        found = _HEADING.match(line)
        if found:
            title = found.group("title").strip().lower().rstrip(":")
            inside = title == heading or title.startswith(f"{heading} (")
            continue
        if not inside:
            continue
        item = _ITEM.match(line)
        if item:
            items.append(item.group("text").strip())
    return items
