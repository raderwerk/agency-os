## Wat

<wat verandert er, in twee tot drie zinnen>

## Waarom

<welk issue, welk probleem; link naar het Linear-issue>

## Bewijs

<testuitvoer, CI-run, screenshot of documentlink — een vinkje zonder link telt niet>

## Definition of Done

- [ ] Artefact bestaat op de afgesproken plek: code met een test, of een document
- [ ] Elk acceptatiecriterium uit het issue is toetsbaar afgevinkt, met bewijs
- [ ] `python -m compileall -q agency_os tests` en `python -m unittest discover -s tests -v` lokaal groen
- [ ] Review door een tweede rol, uit een andere modelfamilie dan de uitvoerder
- [ ] README of AGENTS.md bijgewerkt als dit de werkwijze verandert
- [ ] Geen geheim, token of credential in de diff

## Poort

<welke poort dit passeert, en wie de menselijke goedkeurder is — zie AGENTS.md>
