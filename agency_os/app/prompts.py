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
    extra_context: Mapping[str, str] | None = None,
) -> str:
    """Stelt de volledige prompt samen voor één run van één rol op één issue."""
    extra = dict(extra_context or {})
    blocks = [
        _read(SKELETON_FILE),
        _read(role.prompt_path),
        _issue_block(issue, role=role, run_id=run_id),
        _criteria_block(issue.description or ""),
        _repo_block(cfg, issue, override=extra.pop("agents_md", None)),
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


def _criteria_block(description: str) -> str:
    criteria = acceptance_criteria(description)
    dod = dod_items(description)
    lines = ["## Acceptatiecriteria en Definition of Done", ""]
    if criteria:
        lines += ["Acceptatiecriteria:", ""] + [f"{i}. {item}" for i, item in enumerate(criteria, 1)] + [""]
    else:
        lines += ["Acceptatiecriteria: niet in het issue gevonden. Vraag ernaar in plaats van ze te verzinnen.", ""]
    if dod:
        lines += ["Definition of Done:", ""] + [f"- [ ] {item}" for item in dod]
    else:
        lines += ["Definition of Done: niet in het issue gevonden. Vraag ernaar."]
    lines += ["", f"Vink alleen af wat je met een link in dezelfde comment kunt bewijzen ({len(dod)} punten)."]
    return "\n".join(lines)


def _repo_block(cfg: Any, issue: Any, *, override: Optional[str]) -> str:
    """De AGENTS.md van de doelrepo, uit de lokale clone onder `repo_root`."""
    repo = getattr(issue, "repo", None)
    if override:
        return f"## Repo-instructies ({repo or 'onbekend'})\n\n{override}"
    if not repo:
        return ""
    root = Path(getattr(cfg.executors, "repo_root", Path.home()))
    agents = root / repo.split("/")[-1] / "AGENTS.md"
    text = _read(agents)
    if not text:
        return (
            f"## Repo-instructies ({repo})\n\n"
            f"Niet gevonden op {agents}. Lees AGENTS.md in de worktree voordat je iets wijzigt."
        )
    return f"## Repo-instructies ({repo}, uit {agents.name})\n\n{text}"


_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*$")
_ITEM = re.compile(r"^\s*(?:[-*+]\s+(?:\[[ xX]\]\s*)?|\d+[.)]\s+)(?P<text>.+?)\s*$")


def _section_items(description: str, heading: str) -> list[str]:
    """Opsommingsregels onder een kop, ongeacht kopniveau of hoofdletters."""
    items: list[str] = []
    inside = False
    for line in (description or "").splitlines():
        found = _HEADING.match(line)
        if found:
            inside = found.group("title").strip().lower().rstrip(":") == heading
            continue
        if not inside:
            continue
        item = _ITEM.match(line)
        if item:
            items.append(item.group("text").strip())
    return items
