"""Het geheugen van de Spil: sqlite, WAL, vooruit-genummerde migraties.

De tabel `claims` met zijn gedeeltelijke unieke index is de echte
gelijktijdigheidsgarantie binnen dit proces. Het label `run/bezet` in Linear is
het signaal naar buiten en de spec is er eerlijk over dat dat een benadering is
en geen slot (8.2). Allebei worden gebruikt, geen van beide alleen vertrouwd.

`Store` is zelf een `MutationSink`: hij wordt aan de client meegegeven en
schrijft daarmee laag 3 van het handelingenlogboek (spec hoofdstuk 9).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .models import Artifact, MutationRecord, RunRecord

__all__ = ["Store", "iso", "parse_iso"]

SCHEMA_VERSION = 3

#: Zoveel echte runs van dezelfde rol op hetzelfde issue laat één dag toe
#: (spec 8.6). Staat hier omdat `loops_on` hem nodig heeft; de lusdetectie zelf
#: leest hem uit `app.routing.MAX_ROLE_RUNS_PER_DAY`, en een test bewaakt dat de
#: twee getallen niet uit elkaar lopen -- `app` mag niet uit `linear` importeren
#: en andersom al helemaal niet.
LOOP_LIMIT = 3

_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS claims (
  issue_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  issue_identifier TEXT NOT NULL,
  role TEXT,
  claimed_at TEXT NOT NULL,
  released_at TEXT,
  outcome TEXT,
  PRIMARY KEY (issue_id, run_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_claim_per_issue
  ON claims(issue_id) WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  issue_id TEXT NOT NULL, issue_identifier TEXT NOT NULL, team_key TEXT NOT NULL,
  rol TEXT NOT NULL, model TEXT NOT NULL, executor TEXT NOT NULL,
  klant TEXT, dienst TEXT,
  gestart TEXT NOT NULL, geeindigd TEXT, duur_s REAL NOT NULL DEFAULT 0,
  beurten INTEGER NOT NULL DEFAULT 0,
  tokens_in INTEGER NOT NULL DEFAULT 0, tokens_uit INTEGER NOT NULL DEFAULT 0,
  cache_lees INTEGER NOT NULL DEFAULT 0,
  kosten_usd REAL NOT NULL DEFAULT 0, kosten_eur REAL NOT NULL DEFAULT 0,
  dod TEXT, uitkomst TEXT NOT NULL, volgende_status TEXT, pr_url TEXT,
  metered INTEGER NOT NULL DEFAULT 1,
  artefacten TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS runs_by_day ON runs(substr(gestart,1,10));
CREATE INDEX IF NOT EXISTS runs_by_issue ON runs(issue_id);

CREATE TABLE IF NOT EXISTS mutations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL, run_id TEXT, mutation TEXT NOT NULL, entity_id TEXT NOT NULL,
  variables_digest TEXT NOT NULL, variables_summary TEXT NOT NULL,
  result_id TEXT, ok INTEGER NOT NULL, error TEXT, dry_run INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS mutations_by_entity ON mutations(entity_id, at);

CREATE TABLE IF NOT EXISTS gate_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id TEXT NOT NULL, gate_state TEXT NOT NULL,
  card_comment_id TEXT, card_at TEXT,
  decided_at TEXT, outcome TEXT, token TEXT, source TEXT, source_id TEXT,
  actor_id TEXT, actor_name TEXT, valid INTEGER NOT NULL, refusal TEXT,
  applied_at TEXT, rejections INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS gate_events_by_issue ON gate_events(issue_id, gate_state);

CREATE TABLE IF NOT EXISTS sessions (
  issue_id TEXT NOT NULL, run_id TEXT NOT NULL, executor TEXT NOT NULL,
  session_id TEXT, trigger_comment_id TEXT, triggered_at TEXT NOT NULL,
  last_status TEXT, strikes INTEGER NOT NULL DEFAULT 0, closed_at TEXT,
  PRIMARY KEY (issue_id, run_id)
);

CREATE TABLE IF NOT EXISTS role_runs (
  issue_id TEXT NOT NULL, role TEXT NOT NULL, day TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (issue_id, role, day)
);

CREATE TABLE IF NOT EXISTS heartbeats (
  at TEXT PRIMARY KEY, cycle INTEGER NOT NULL, comment_id TEXT, runs_today INTEGER,
  cost_eur_today REAL, queue_len INTEGER
);
"""


# Migratie 2: een aangestoten native sessie moet ook ná een herstart terug te
# vertalen zijn naar een run. Zonder rol, model en status is de bon niet genoeg
# om de sessie af te maken, want de statustabel kent geen regel voor de status
# waarin het issue tijdens de run staat.
_MIGRATION_2 = (
    ("sessions", "role", "ALTER TABLE sessions ADD COLUMN role TEXT"),
    ("sessions", "model_key", "ALTER TABLE sessions ADD COLUMN model_key TEXT"),
    ("sessions", "state", "ALTER TABLE sessions ADD COLUMN state TEXT"),
)

# Migratie 3: de lusdetectie telt rolpogingen, en een poging waarbij de laan
# zelf niet startte is geen poging van de rol. `count` blijft alles wat er
# geclaimd is, `infra` is het deel daarvan dat nooit bij een model aankwam; het
# verschil is wat de lusdetectie ziet. Aftrekken van `count` zou goedkoper zijn
# en zou het spoor uitwissen dat er wél iets geprobeerd is.
_MIGRATION_3 = (
    ("role_runs", "infra", "ALTER TABLE role_runs ADD COLUMN infra INTEGER NOT NULL DEFAULT 0"),
)


def iso(moment: Optional[datetime]) -> Optional[str]:
    """Tijdzonebewuste UTC -> ISO-8601 met Z. None blijft None."""
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class Store:
    """Alle sqlite-toegang. Niemand anders opent deze database."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> None:
        """Vooruit-genummerde migraties; nooit terug."""
        self.conn.executescript(_MIGRATION_1)
        for table, column, statement in (*_MIGRATION_2, *_MIGRATION_3):
            if column not in self._columns(table):
                self.conn.execute(statement)
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}

    def close(self) -> None:
        self.conn.close()

    # ---------------- meta ----------------

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def cache_id(self, kind: str, name: str, value: Optional[str] = None) -> Optional[str]:
        """Naar identifier of naam opgeloste uuid's, voor de duur van dit proces.

        idmap.json is geen invoer (architectuur 18.7): alles wordt bij het
        starten opgelost en hier gecachet.
        """
        key = f"cache:{kind}:{name}"
        if value is not None:
            self.set_meta(key, value)
            return value
        return self.get_meta(key)

    # ---------------- mutations (MutationSink) ----------------

    def record(self, m: MutationRecord) -> None:
        """Laag 3 van het handelingenlogboek. De client roept dit bij elke schrijfactie aan."""
        self.conn.execute(
            "INSERT INTO mutations(at, run_id, mutation, entity_id, variables_digest, "
            "variables_summary, result_id, ok, error, dry_run) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (iso(m.at), m.run_id, m.mutation, m.entity_id, m.variables_digest,
             json.dumps(dict(m.variables_summary), sort_keys=True, default=str),
             m.result_id, int(m.ok), m.error, int(m.dry_run)),
        )

    def mutations_for(self, entity_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM mutations WHERE entity_id = ? ORDER BY at", (entity_id,)))

    # ---------------- claims ----------------

    def open_claim(self, issue_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM claims WHERE issue_id = ? AND released_at IS NULL", (issue_id,)
        ).fetchone()

    def open_claims(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM claims WHERE released_at IS NULL"))

    def insert_claim(self, issue_id: str, run_id: str, issue_identifier: str,
                     claimed_at: datetime, role: Optional[str] = None) -> bool:
        """True als deze run het slot kreeg, False als iemand anders het al had."""
        try:
            self.conn.execute(
                "INSERT INTO claims(issue_id, run_id, issue_identifier, role, claimed_at) "
                "VALUES(?,?,?,?,?)",
                (issue_id, run_id, issue_identifier, role, iso(claimed_at)),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def release_claim(self, issue_id: str, run_id: str, outcome: str,
                      released_at: Optional[datetime] = None) -> None:
        self.conn.execute(
            "UPDATE claims SET released_at = ?, outcome = ? WHERE issue_id = ? AND run_id = ?",
            (iso(released_at or datetime.now(timezone.utc)), outcome, issue_id, run_id),
        )

    def claim_is_closed(self, issue_id: str, run_id: str) -> bool:
        """True als deze run het issue al gehad heeft en netjes heeft losgelaten.

        Het claimcomment van zo'n run blijft in Linear staan. Zonder deze vraag
        leest de volgende claimer dat comment als een levende tegenstander en
        trekt hij zich terug voor iemand die allang klaar is.
        """
        row = self.conn.execute(
            "SELECT 1 FROM claims WHERE issue_id = ? AND run_id = ? AND released_at IS NOT NULL",
            (issue_id, run_id),
        ).fetchone()
        return row is not None

    def release_stale_claims(self, older_than: datetime,
                             outcome: str = "verweesd") -> list[str]:
        """Sluit elke open claim die ouder is dan `older_than`. Geeft de issue-ids terug.

        Een proces dat omvalt laat zijn rij open staan, en die rij houdt het
        issue voorgoed onclaimbaar (architectuur 6.1 en 13: de volgende start
        verzoent).
        """
        rows = list(self.conn.execute(
            "SELECT issue_id, run_id FROM claims WHERE released_at IS NULL AND claimed_at < ?",
            (iso(older_than),)))
        for row in rows:
            self.release_claim(row["issue_id"], row["run_id"], outcome)
        return [row["issue_id"] for row in rows]

    def has_claim(self, issue_id: str, run_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM claims WHERE issue_id = ? AND run_id = ?", (issue_id, run_id)
        ).fetchone()
        return row is not None

    # ---------------- runs ----------------

    def insert_run(self, run: RunRecord) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs(run_id, issue_id, issue_identifier, team_key, rol, "
            "model, executor, klant, dienst, gestart, geeindigd, duur_s, beurten, tokens_in, "
            "tokens_uit, cache_lees, kosten_usd, kosten_eur, dod, uitkomst, volgende_status, "
            "pr_url, metered, artefacten) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run.run_id, run.issue_id, run.issue_identifier, run.team_key, run.rol, run.model,
             run.executor, run.klant, run.dienst, iso(run.gestart), iso(run.geeindigd),
             run.duur_s, run.beurten, run.tokens_in, run.tokens_uit, run.cache_lees,
             run.kosten_usd, run.kosten_eur, run.dod, run.uitkomst, run.volgende_status,
             run.pr_url, int(run.metered),
             json.dumps([{"type": a.type, "url": a.url, "label": a.label}
                         for a in run.artefacten])),
        )

    def runs_between(self, since: date, until: date) -> list[RunRecord]:
        rows = self.conn.execute(
            "SELECT * FROM runs WHERE substr(gestart,1,10) BETWEEN ? AND ? ORDER BY gestart",
            (since.isoformat(), until.isoformat()),
        )
        return [_row_to_run(row) for row in rows]

    def runs_on(self, day: date) -> list[RunRecord]:
        return self.runs_between(day, day)

    def cost_eur_for_issue(self, issue_id: str) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(kosten_eur), 0) AS total FROM runs WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()
        return float(row["total"])

    # ---------------- gate events ----------------

    def record_gate_event(self, *, issue_id: str, gate_state: str,
                          card_comment_id: Optional[str], card_at: Optional[datetime],
                          decided_at: Optional[datetime], outcome: Optional[str],
                          token: Optional[str], source: Optional[str], source_id: Optional[str],
                          actor_id: Optional[str], actor_name: Optional[str], valid: bool,
                          refusal: Optional[str], rejections: int = 0) -> int:
        cursor = self.conn.execute(
            "INSERT INTO gate_events(issue_id, gate_state, card_comment_id, card_at, decided_at, "
            "outcome, token, source, source_id, actor_id, actor_name, valid, refusal, rejections) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (issue_id, gate_state, card_comment_id, iso(card_at), iso(decided_at), outcome, token,
             source, source_id, actor_id, actor_name, int(valid), refusal, rejections),
        )
        return int(cursor.lastrowid or 0)

    def mark_gate_applied(self, event_id: int, applied_at: datetime) -> None:
        self.conn.execute("UPDATE gate_events SET applied_at = ? WHERE id = ?",
                          (iso(applied_at), event_id))

    def gate_decision_seen(self, issue_id: str, gate_state: str, source_id: str) -> bool:
        """True als deze exacte bron (comment-id of label) al verwerkt is."""
        row = self.conn.execute(
            "SELECT 1 FROM gate_events WHERE issue_id = ? AND gate_state = ? AND source_id = ? "
            "AND applied_at IS NOT NULL",
            (issue_id, gate_state, source_id),
        ).fetchone()
        return row is not None

    def rejection_count(self, issue_id: str, gate_state: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM gate_events WHERE issue_id = ? AND gate_state = ? "
            "AND outcome = 'afgekeurd' AND valid = 1 AND applied_at IS NOT NULL",
            (issue_id, gate_state),
        ).fetchone()
        return int(row["n"])

    def gate_events_between(self, since: date, until: date) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM gate_events WHERE substr(COALESCE(decided_at, card_at),1,10) "
            "BETWEEN ? AND ?", (since.isoformat(), until.isoformat())))

    # ---------------- sessions ----------------

    def upsert_session(self, *, issue_id: str, run_id: str, executor: str,
                       session_id: Optional[str], trigger_comment_id: Optional[str],
                       triggered_at: datetime, last_status: Optional[str] = None,
                       strikes: int = 0, closed_at: Optional[datetime] = None,
                       role: Optional[str] = None, model_key: Optional[str] = None,
                       state: Optional[str] = None) -> None:
        self.conn.execute(
            "INSERT INTO sessions(issue_id, run_id, executor, session_id, trigger_comment_id, "
            "triggered_at, last_status, strikes, closed_at, role, model_key, state) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(issue_id, run_id) DO UPDATE SET session_id=excluded.session_id, "
            "last_status=excluded.last_status, strikes=excluded.strikes, "
            "closed_at=excluded.closed_at, "
            "role=COALESCE(excluded.role, sessions.role), "
            "model_key=COALESCE(excluded.model_key, sessions.model_key), "
            "state=COALESCE(excluded.state, sessions.state)",
            (issue_id, run_id, executor, session_id, trigger_comment_id, iso(triggered_at),
             last_status, strikes, iso(closed_at), role, model_key, state),
        )

    def get_session(self, issue_id: str, run_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE issue_id = ? AND run_id = ?", (issue_id, run_id)
        ).fetchone()

    def open_sessions(self) -> list[sqlite3.Row]:
        """Elke aangestoten native sessie die nog geen einde kreeg.

        Hiermee overleeft een Codex- of Cursorsessie een herstart: zonder deze
        rij bestaat het bewijs van een betaalde sessie alleen in het geheugen van
        het proces dat hem startte.
        """
        return list(self.conn.execute(
            "SELECT * FROM sessions WHERE closed_at IS NULL ORDER BY triggered_at"))

    def close_session(self, issue_id: str, run_id: str, closed_at: datetime) -> None:
        self.conn.execute(
            "UPDATE sessions SET closed_at = ? WHERE issue_id = ? AND run_id = ?",
            (iso(closed_at), issue_id, run_id))

    # ---------------- loop detection ----------------

    def bump_role_run(self, issue_id: str, role: str, day: date) -> int:
        """Boek een claim van deze rol op deze dag. Geeft het aantal échte runs terug."""
        self.conn.execute(
            "INSERT INTO role_runs(issue_id, role, day, count) VALUES(?,?,?,1) "
            "ON CONFLICT(issue_id, role, day) DO UPDATE SET count = count + 1",
            (issue_id, role, day.isoformat()),
        )
        return self.role_run_count(issue_id, role, day)

    def discount_role_run(self, issue_id: str, role: str, day: date) -> int:
        """Streep de zojuist geboekte poging weg: de laan startte niet.

        Een run die afketste op een vlag die de CLI niet kent, een zandbak die
        weigert of een binair dat er niet is, heeft geen rolpoging opgeleverd --
        er is geen model aan te pas gekomen en er is dus niets waar de rol in
        vast kan lopen. Zo'n poging mag de dagbeurten van de rol niet opeten.
        Het claimspoor in `count` blijft staan, want er is wel geld en tijd aan
        opgegaan; alleen de teller die de lusdetectie leest schuift niet op.
        """
        self.conn.execute(
            "UPDATE role_runs SET infra = min(infra + 1, count) "
            "WHERE issue_id = ? AND role = ? AND day = ?",
            (issue_id, role, day.isoformat()),
        )
        return self.role_run_count(issue_id, role, day)

    def role_run_count(self, issue_id: str, role: str, day: date) -> int:
        """Het aantal runs waarin deze rol vandaag echt aan het werk is geweest."""
        row = self.conn.execute(
            "SELECT count - infra AS echt FROM role_runs "
            "WHERE issue_id = ? AND role = ? AND day = ?",
            (issue_id, role, day.isoformat()),
        ).fetchone()
        return max(0, int(row["echt"])) if row else 0

    def role_run_attempts(self, issue_id: str, role: str, day: date) -> int:
        """Alles wat er geclaimd is, inclusief de runs die nooit bij een model aankwamen."""
        row = self.conn.execute(
            "SELECT count FROM role_runs WHERE issue_id = ? AND role = ? AND day = ?",
            (issue_id, role, day.isoformat()),
        ).fetchone()
        return int(row["count"]) if row else 0

    def loops_on(self, day: date, limit: int = LOOP_LIMIT) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM role_runs WHERE day = ? AND count - infra >= ?",
            (day.isoformat(), int(limit)),
        ).fetchone()
        return int(row["n"])

    # ---------------- heartbeats ----------------

    def record_heartbeat(self, *, at: datetime, cycle: int, comment_id: Optional[str],
                         runs_today: int, cost_eur_today: float, queue_len: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO heartbeats(at, cycle, comment_id, runs_today, "
            "cost_eur_today, queue_len) VALUES(?,?,?,?,?,?)",
            (iso(at), cycle, comment_id, runs_today, cost_eur_today, queue_len),
        )

    def last_heartbeat(self) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM heartbeats ORDER BY at DESC LIMIT 1").fetchone()

    def last_heartbeat_at(self) -> Optional[datetime]:
        """Het moment van de laatste hartslag, of None als er nog geen is.

        De wachthond wil een datetime, geen rij: het omzetten van iso hoort in
        deze laag, niet bij elke aanroeper.
        """
        row = self.last_heartbeat()
        return parse_iso(row["at"]) if row is not None else None


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    artefacten = tuple(
        Artifact(type=a.get("type", ""), url=a.get("url", ""), label=a.get("label", ""))
        for a in json.loads(row["artefacten"] or "[]")
    )
    return RunRecord(
        run_id=row["run_id"], issue_id=row["issue_id"],
        issue_identifier=row["issue_identifier"], team_key=row["team_key"], rol=row["rol"],
        model=row["model"], executor=row["executor"], klant=row["klant"], dienst=row["dienst"],
        gestart=parse_iso(row["gestart"]) or datetime.now(timezone.utc),
        geeindigd=parse_iso(row["geeindigd"]), duur_s=float(row["duur_s"]),
        beurten=int(row["beurten"]), tokens_in=int(row["tokens_in"]),
        tokens_uit=int(row["tokens_uit"]), cache_lees=int(row["cache_lees"]),
        kosten_usd=float(row["kosten_usd"]), kosten_eur=float(row["kosten_eur"]),
        dod=row["dod"] or "-", uitkomst=row["uitkomst"], volgende_status=row["volgende_status"],
        pr_url=row["pr_url"], artefacten=artefacten, metered=bool(row["metered"]),
    )
