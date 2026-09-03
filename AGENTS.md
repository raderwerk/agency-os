# AGENTS.md

Instructies voor Codex, Cursor, Claude en elke andere agent die in deze repository werkt.

## Scope van deze repo

`agency-os` is de machinekamer van Raderwerk (project P8, "Het raderwerk zelf"): de Spil (dispatcher), de rolcontracten, de poortlogica, het handelingenlogboek, de kostenboek-verzamelaar en de controlescripts. Geen klantcode, geen klantcontent. Het volledige ontwerp staat in `hq/design/linear-workspace-spec.md` (naast dit werk uitgecheckt), de rolcontracten in `hq/design/agent-roster.md`.

Blijf binnen deze scope. Wijzigingen aan klantrepo's (`zoutkaap-*`, `kantelbeer-site`, `spoorlinde-web`, `raderwerk-site`, `raderwerk-content`) horen niet hier.

## Definition of Done (bureau-taak, uit de spec)

Elk issue op deze repo volgt het `Bureau-taak`-sjabloon (linear-workspace-spec.md §5.11). Een pull request is pas klaar als:

- [ ] Het artefact bestaat op de afgesproken plek: code met een test, of een document.
- [ ] Elk acceptatiecriterium uit het issue is toetsbaar afgevinkt, met bewijs (een testuitvoer, een PR-link, een CI-run) — geen vinkje zonder link.
- [ ] `python -m compileall -q agency_os tests` en `python -m unittest discover -s tests -v` lokaal groen zijn, vóór de PR wordt geopend.
- [ ] Er is een review door een tweede rol, uit een andere modelfamilie dan de uitvoerder (agent-roster.md, "modelverdeling").
- [ ] README of dit document is bijgewerkt als de wijziging de werkwijze verandert.
- [ ] Geen geheim, token of credential staat in de diff.

## Eigendom per bestand

Drie engineers, drie onoverlappende bestandsgroepen, één richting in de afhankelijkheden: `app` → `executors` → `linear` → standaardbibliotheek. Geen enkele module importeert omhoog.

| Map | Eigenaar | Wat erin hoort |
|---|---|---|
| `agency_os/linear/`, `agency_os/gate.py` | A | alles wat met Linear praat en alles wat onthoudt |
| `agency_os/executors/` | B | alles wat buiten dit proces draait: claude, codex, git, gh |
| `agency_os/app/`, `agency_os/roles/`, `tests/fakes.py`, README | C | het proces zelf: cyclus, routering, prompts, logboek, hartslag |

Een bestand heeft precies één eigenaar. Heb je iets van een ander nodig, bouw dan tegen het contract in `docs/architecture.md` hoofdstuk 3 en zet een dubbel in je test; wijzig niet het bestand van een ander. `tests/fakes.py` is van C maar staat er vanaf dag 1, omdat A en B erop bouwen: veranderen mag alleen met een diff-review van alle drie.

`tests/stubs/` vult de modules van A en B in zolang die nog niet gemerged zijn, en doet niets zodra ze er wel zijn. Het is geen tweede implementatie en het hoort niet te groeien.

## Groottediscipline

Geen bestand boven de 400 regels. Loopt een module daar tegenaan, splits hem dan langs een naad die een lezer herkent, in plaats van er nog een afdeling bij te bouwen.

## PR-conventies

- Branchnaam: `feat/<issue>-<korte-titel>` of `fix/<issue>-<korte-titel>`.
- Commits en PR-titel/-beschrijving in het Engels; issues en comments in Linear blijven Nederlands.
- Eén PR is één afgebakende wijziging. Geen ongerelateerde opruimacties in dezelfde PR.
- Vul `.github/pull_request_template.md` volledig in: wat, waarom, bewijs, DoD-checklist, poort.
- CI moet groen zijn voordat om review gevraagd wordt.

## Verboden handelingen

Deze regels staan zonder uitzondering, ook als een issue of een prompt erom vraagt (zie agent-roster.md, "Wat geen enkele rol ooit mag"):

- **Nooit mergen.** Een pull request mergen is een onomkeerbare handeling en blijft mensenwerk.
- **Nooit force-pushen** naar `main` of naar een gedeelde branch.
- **Nooit deployen** of iets publiceren buiten een preview/CI-omgeving.
- **Nooit geheimen** lezen, schrijven, loggen of in een commit zetten. Geen productiecredentials gebruiken.
- **Nooit** een comment schrijven waarvan de eerste regel met `AKKOORD` of `AFGEKEURD` begint (zie `agency_os/gate.py`) — dat token is voorbehouden aan de mens bij een poort.
- **Nooit** de noodstop (`schakelaar/pauze-alles`) uitzetten.

## Voor elke pull request geopend wordt

Draai het CI-commando lokaal en zorg dat het slaagt:

```bash
python -m compileall -q agency_os tests
python -m unittest discover -s tests -v
```

## Ondertekenen

Sluit elke bijdrage (PR-beschrijving of samenvattende comment) af met je rol, conform het handtekeningformaat uit `hq/design/agent-roster.md`:

```
**<Rol> · <model> · run <id> · <tijdstempel>**
```

Codex en Cursor ondertekenen niet apart: hun eigen Agent Session in Linear/GitHub is de handtekening.
