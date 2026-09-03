"""Het handelingenlogboek: één json-object per regel, alleen toevoegen.

`<state_dir>/logbook/YYYY-MM-DD.jsonl`, buiten elke repo (spec hoofdstuk 9,
laag 3). De Logbook wordt bij de client geregistreerd als `MutationSink`, zodat
elke schrijfactie hier langskomt vóór de aanroeper het resultaat ziet, en wordt
daarnaast rechtstreeks door de planner gebruikt voor alles wat geen mutatie is.

Er staat nooit een waarde in die een geheim kan zijn: van een mutatie bewaren we
de allowlist-samenvatting en de sha256 van de variabelen, zoals de client die
aanlevert.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

KINDS = frozenset(
    {"poll", "claim", "route", "run", "mutation", "gate", "heartbeat", "halt", "error", "skip"}
)


class Logbook:
    """Schrijft regels weg per dag. Draadveilig, want de runs draaien parallel."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path_for(self, day: date) -> Path:
        """Het bestand van die dag."""
        return self.directory / f"{day.isoformat()}.jsonl"

    def record(self, m: Any) -> None:
        """MutationSink: elke schrijfactie van de client komt hier binnen."""
        self._append(
            "mutation",
            at=m.at,
            run_id=m.run_id,
            issue=m.entity_id,
            payload={
                "mutation": m.mutation,
                "entity_id": m.entity_id,
                "result_id": m.result_id,
                "ok": m.ok,
                "error": m.error,
                "dry_run": m.dry_run,
                "variables_summary": dict(m.variables_summary),
                "variables_digest": m.variables_digest,
            },
        )

    def write(self, kind: str, *, run_id: str | None, issue: str | None, payload: dict) -> None:
        """Eén gebeurtenis wegschrijven. Onbekende soort is een fout, geen regel."""
        if kind not in KINDS:
            raise ValueError(f"onbekende logboeksoort {kind!r}; toegestaan: {sorted(KINDS)}")
        self._append(kind, at=datetime.now(timezone.utc), run_id=run_id, issue=issue, payload=payload)

    def export(self, since: date, until: date) -> str:
        """Alle regels van `since` tot en met `until`, in volgorde van de dagen."""
        chunks: list[str] = []
        day = since
        while day <= until:
            path = self.path_for(day)
            if path.is_file():
                chunks.append(path.read_text(encoding="utf-8").rstrip("\n"))
            day += timedelta(days=1)
        return "\n".join(chunk for chunk in chunks if chunk)

    def _append(self, kind: str, *, at: datetime, run_id: Optional[str], issue: Optional[str], payload: dict) -> None:
        moment = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        line = json.dumps(
            {
                "at": moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "kind": kind,
                "run_id": run_id,
                "issue": issue,
                "payload": payload,
            },
            ensure_ascii=False,
            default=str,
        )
        with self._lock:
            with self.path_for(moment.astimezone(timezone.utc).date()).open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
