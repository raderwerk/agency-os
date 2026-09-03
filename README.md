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

## De Spil draaien

Eerst een `~/.config/raderwerk/spil.env`. Zes sleutels hebben geen standaardwaarde en zonder een daarvan start de Spil niet; hij zegt dan welke ontbreekt en stopt met afloopcode `2`.

```bash
mkdir -p ~/.config/raderwerk && chmod 700 ~/.config/raderwerk
cat > ~/.config/raderwerk/spil.env <<'EOF'
SPIL_LINEAR_API_KEY=lin_api_...
SPIL_DISPATCHER_USER_ID=<uuid van het account waarmee de Spil schrijft>
SPIL_APPROVER_IDS=<uuid>,<uuid>
SPIL_FX_USD_EUR=0.92
SPIL_FX_SOURCE=ECB
SPIL_FX_DATE=2026-09-01
SPIL_REPO_ROOT=~/Developer/Personal/Raderwerk
EOF
chmod 600 ~/.config/raderwerk/spil.env
```

De uuids haal je uit de workspace zelf: `viewer { id }` is de dispatcher, en de goedkeurders zijn de mensen uit D02. Draaien de Spil en de goedkeurder onder hetzelfde account, dan kan de Spil zijn eigen poort niet openen -- dat is de bedoeling, maar het betekent ook dat het akkoord-kanaal pas te demonstreren is met een eigen sleutel voor de dispatcher.

```bash
python -m agency_os status [--json]                 # herkomst van de config, hartslag, issueteller, uitvoerders
python -m agency_os dry-run [--issue WV-207]        # volledige cyclus, geen enkele schrijfactie
python -m agency_os run --once [--issue WV-207]     # precies één cyclus
python -m agency_os run --loop --interval 60        # de dispatcher
python -m agency_os heartbeat [--watchdog]          # hartslag, of de losse wachthond
python -m agency_os ledger [--since D] [--format markdown|json] [--logbook]
```

Afloopcodes: `0` in orde, `1` ongezond of geweigerd, `2` configuratiefout, `130` onderbroken. De wachthond hoort in een ánder proces: `*/10 * * * * python -m agency_os heartbeat --watchdog`.

Begin altijd met `status` en daarna `dry-run`. Een droogloop doet de hele cyclus -- lezen, poorten beoordelen, routeren, de prompt bouwen -- en drukt per issue af welke mutatie hij zou hebben gedaan, plus de routering: welk issue naar welke rol, welk model en welke laan zou gaan. Er gaat geen enkele mutatie over de lijn; de schrijfmethodes komen niet verder dan het handelingenlogboek. Een droogloop heeft ook zijn eigen geheugen: hij schrijft in `<SPIL_STATE_DIR>/dry-run.sqlite3` en raakt de claims, de lusdetectie en het Kostenboek van de echte dispatcher niet aan. Pas als die uitkomst klopt, is `run --once` aan de beurt.

`run --loop` is het echte werk: één proces, één cyclus per `SPIL_INTERVAL_S`, cycli overlappen nooit. SIGINT en SIGTERM laten de lopende cyclus aflopen en stoppen daarna.

## Stoppen

Er zijn drie remmen, van zacht naar hard, en ze zitten alle drie in Linear en niet in de code. Dat is met opzet: wie moet ingrijpen heeft dan geen shell nodig.

| Wat | Waar | Wat er gebeurt |
|---|---|---|
| `schakelaar/pauze` | op één issue | dat issue wordt overgeslagen; de rest loopt door |
| `schakelaar/pauze-alles` | op het bedieningspaneel (`SPIL_PANEL_ISSUE`, standaard WV-156) | binnen één pollronde start er niets meer; elke openstaande claim gaat terug naar `run/wachtrij` met één afbreekcomment per geraakt issue. Een run die op dat moment al draait, loopt af — er is geen procesregister om hem mee af te schieten, dus dat kan tot `SPIL_RUN_TIMEOUT_S` (standaard 30 minuten) duren |
| Ctrl-C / SIGTERM | op het proces | de lopende cyclus loopt af, daarna stopt de lus |

`schakelaar/pauze-alles` is de noodrem. De Spil mag dat label wél zetten en nooit weghalen: het weghalen is geen belofte in een prompt maar een ontbrekende mogelijkheid in `update_issue` (slot 5). Aanzetten kost dus één klik in Linear, uitzetten is per definitie mensenwerk.

Vergeet na een noodstop niet dat het label blijft staan. `python -m agency_os status` zegt het met zoveel woorden ("schakelaar/pauze-alles staat aan: er start niets") en geeft afloopcode `1`, zodat een cron of een healthcheck erover valt.

## Configuratie

Volgorde van winnen: vlaggen op de commandoregel, dan `os.environ`, dan `~/.config/raderwerk/spil.env`, dan de standaardwaarde. Het bestand is `KEY=VALUE` met `#`-commentaar, staat niet in deze repo en wordt nooit geprint: `status` toont alleen wáár de sleutel vandaan kwam.

| Sleutel | Standaard | |
|---|---|---|
| `SPIL_LINEAR_API_KEY` | — | verplicht; header is `Authorization: <sleutel>`, zonder `Bearer` |
| `SPIL_DISPATCHER_USER_ID` | — | verplicht; het account waarmee de Spil schrijft |
| `SPIL_APPROVER_IDS` | — | verplicht; komma-gescheiden Linear-user-uuids uit D02 |
| `SPIL_FX_USD_EUR`, `SPIL_FX_SOURCE`, `SPIL_FX_DATE` | — | verplicht; er staat geen koers in de code |
| `SPIL_PANEL_ISSUE` | `WV-156` | het bedieningspaneel |
| `SPIL_STATE_DIR` | `~/.local/state/raderwerk` | sqlite, logboek en ruwe runuitvoer |
| `SPIL_INTERVAL_S` / `SPIL_MAX_CLAIMS_PER_CYCLE` / `SPIL_MAX_CONCURRENT_RUNS` | `60` / `4` / `2` | |
| `SPIL_HEARTBEAT_EVERY_CYCLES` / `SPIL_WATCHDOG_MAX_AGE_S` | `15` / `1800` | |
| `SPIL_ISSUE_BUDGET` | `200,220,225` | waarschuwen, alleen incidenten, noodstop |
| `SPIL_ALLOW_FABLE` | `false` | staat hij uit, dan zakt Fable naar Opus 5 mét die zin in de comment |
| `SPIL_PRICES` | ingebouwde tabel | `model:in:uit:cache` per miljoen tokens, lijstprijs |
| `SPIL_DRY_RUN` | `false` | |
| `SPIL_LINEAR_ENDPOINT` | `https://api.linear.app/graphql` | |
| `SPIL_CONFIG_FILE` | `~/.config/raderwerk/spil.env` | alleen uit `os.environ`; in het bestand zelf zou hij naar zichzelf wijzen |
| `SPIL_REPO_ROOT` | `~/Developer/Personal/Raderwerk` | waar de klonen van de doelrepo's staan |
| `SPIL_WORKTREE_ROOT` | `<repo_root>/.worktrees` | moet binnen `SPIL_REPO_ROOT` liggen en nooit onder een verboden pad; allebei fataal bij het opstarten |
| `SPIL_CLAIM_SETTLE_S` | `5` | het venster waarin een tweede claimer zich terugtrekt |
| `SPIL_RUN_TIMEOUT_S` / `SPIL_NATIVE_SESSION_TIMEOUT_S` | `1800` / `3600` | |
| `SPIL_CLAUDE_BIN` / `SPIL_CODEX_BIN` / `SPIL_GH_BIN` / `SPIL_GIT_BIN` | `claude` / `codex` / `gh` / `git` | `status` zegt of ze in PATH staan |

Alles wat de Spil onthoudt staat buiten elke repo, onder `SPIL_STATE_DIR`: `spil.sqlite3` (claims, runs, mutaties, poortbeslissingen), `logbook/JJJJ-MM-DD.jsonl` (het handelingenlogboek, één json-object per regel) en `runs/<run-id>/` (de ruwe uitvoer van een model, die nooit voluit in Linear belandt).

## Hoe het in elkaar zit

Het ontwerp staat voluit in [docs/architecture.md](./docs/architecture.md). De korte versie: `agency_os/linear/` praat met Linear en onthoudt alles, `agency_os/executors/` draait alles buiten dit proces (claude, codex, git, gh), en `agency_os/app/` is het proces zelf: de cyclus, de routering, de prompts, het logboek en de hartslag. De afhankelijkheid loopt één kant op: app → executors → linear → standaardbibliotheek.

De routeringstabel is data, geen code: `agency_os/roles/routing.json` koppelt (bord, status, labels) aan een rol, en `agency_os/roles/*.md` zijn de rolprompts. Een rol toevoegen is een regel json en een markdownbestand, geen `if`.

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
