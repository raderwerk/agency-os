# CLAUDE.md

Zie [AGENTS.md](./AGENTS.md).

Dezelfde regels gelden voor Claude als voor elke andere agent in deze repository:
- scope, Definition of Done en PR-conventies staan in AGENTS.md;
- geen merge, geen force-push, geen deploy, geen geheimen in commits of PR's;
- draai altijd het CI-commando (`python -m compileall -q agency_os tests && python -m unittest discover -s tests -v`) lokaal voordat je een pull request opent;
- teken elke bijdrage af met je rol, conform het handtekeningformaat in `hq/design/agent-roster.md`.
