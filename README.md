# agency-os

De machinekamer van Raderwerk: de Spil (dispatcher), de rolcontracten, de poortlogica, het handelingenlogboek, de kostenboek-verzamelaar en de controlescripts waarmee de werkplaats zichzelf bewijst.

## Doel

Raderwerk is een demonstratiebureau dat draait op AI-agents, met mensen alleen bij de poorten. `agency-os` is het project waar het bureau zichzelf als klant behandelt (project P8, initiative "Het raderwerk zelf"): het bevat geen klantwerk, maar de machine die al het andere klantwerk laat lopen. Het volledige ontwerp staat in `hq/design/linear-workspace-spec.md`, de rolcontracten in `hq/design/agent-roster.md`.

## Klant

Geen. Dit is intern werk (`klant/geen`, `dienst/intern`, `soort/bureau`), niet-factureerbaar, maar wel volledig door dezelfde drie poorten als elk ander engagement — anders bewijst de demo niets over de eigen werkwijze.

## Stack en waarom

Python 3.12, standaardbibliotheek. Geen framework, geen externe dependency, geen packaging-laag: dezelfde keuze als `hq/tools`, waar deze repo qua stijl op aansluit. Een dispatcher die elke minuut draait en de GitHub- en Linear-API's aanroept heeft geen web framework of database nodig, alleen betrouwbare, leesbare scripts. Elke afhankelijkheid die hier bijkomt moet die keuze opnieuw verantwoorden.

## Lokaal draaien

```bash
git clone https://github.com/raderwerk/agency-os.git
cd agency-os
python -m compileall -q agency_os tests   # build: syntaxcontrole
python -m unittest discover -s tests -v   # test
```

Geen installatiestap nodig: alles staat in de standaardbibliotheek. Dit zijn ook precies de twee commando's die CI draait.

## Hoe bijdragen (via pull request)

Al het werk gaat via een pull request; een mens keurt goed bij de poort en merget. Zie [AGENTS.md](./AGENTS.md) voor de volledige scope, de Definition of Done en wat een agent nooit mag.

1. Vertak vanaf `main`.
2. Bouw, met bewijs: elke wijziging aan gedrag komt met een test.
3. Draai lokaal `python -m compileall -q agency_os tests && python -m unittest discover -s tests -v`, groen voordat je een PR opent.
4. Open de pull request met het sjabloon (`.github/pull_request_template.md`) volledig ingevuld.
5. Wacht op groene CI en op review; merge gebeurt uitsluitend door een mens.

## Poorten

Deze repo hangt onder een GitHub-ruleset op `main`: pull request verplicht, verplichte status check `ci`, geen bypass voor agent-tokens. Geen agent — Claude, Codex of Cursor — mag mergen, force-pushen of deployen. Zie hoofdstuk 7 van `hq/design/linear-workspace-spec.md` voor de volledige poortmechaniek op de klantreis en de werkvloer.

## Publieke pagina's

Geen. Deze repo heeft geen GitHub Pages en geen publiek bereikbare pagina, dus de voettekstregel voor fictieve klanten is hier niet van toepassing.
