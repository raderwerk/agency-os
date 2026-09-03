"""Stand-in voor `agency_os.linear.store` (onderdeel A), schema uit spec 5.1.

Het schema is letterlijk dat van de architectuur, inclusief de unieke index die
de echte gelijktijdigheidsgarantie is. De methodes zijn precies die welke C
gebruikt; A mag er meer hebben.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims (
  issue_id TEXT NOT NULL, run_id TEXT NOT NULL, issue_identifier TEXT NOT NULL, role TEXT,
  claimed_at TEXT NOT NULL, released_at TEXT, outcome TEXT, PRIMARY KEY (issue_id, run_id));
CREATE UNIQUE INDEX IF NOT EXISTS one_open_claim_per_issue ON claims(issue_id) WHERE released_at IS NULL;
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, issue_identifier TEXT NOT NULL, team_key TEXT NOT NULL,
  rol TEXT NOT NULL, model TEXT NOT NULL, executor TEXT NOT NULL, klant TEXT, dienst TEXT,
  gestart TEXT NOT NULL, geeindigd TEXT, duur_s REAL NOT NULL DEFAULT 0, beurten INTEGER NOT NULL DEFAULT 0,
  tokens_in INTEGER NOT NULL DEFAULT 0, tokens_uit INTEGER NOT NULL DEFAULT 0, cache_lees INTEGER NOT NULL DEFAULT 0,
  kosten_usd REAL NOT NULL DEFAULT 0, kosten_eur REAL NOT NULL DEFAULT 0, dod TEXT, uitkomst TEXT NOT NULL,
  volgende_status TEXT, pr_url TEXT, metered INTEGER NOT NULL DEFAULT 1, artefacten TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS mutations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, run_id TEXT, mutation TEXT NOT NULL,
  entity_id TEXT NOT NULL, variables_digest TEXT NOT NULL, variables_summary TEXT NOT NULL,
  result_id TEXT, ok INTEGER NOT NULL, error TEXT, dry_run INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS role_runs (
  issue_id TEXT NOT NULL, role TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (issue_id, role, day));
CREATE TABLE IF NOT EXISTS heartbeats (
  at TEXT PRIMARY KEY, cycle INTEGER NOT NULL, comment_id TEXT, runs_today INTEGER,
  cost_eur_today REAL, queue_len INTEGER);
"""


class Store:
    """Sqlite op één bestand. WAL, foreign keys aan."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- meta ---
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))
        self.conn.commit()

    # --- claims ---
    def open_claim(self, issue_id: str, run_id: str, identifier: str, role: Optional[str], at: datetime) -> bool:
        try:
            self.conn.execute(
                "INSERT INTO claims (issue_id, run_id, issue_identifier, role, claimed_at) VALUES (?,?,?,?,?)",
                (issue_id, run_id, identifier, role, at.isoformat()),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def close_claim(self, issue_id: str, run_id: str, outcome: str, at: datetime) -> None:
        self.conn.execute(
            "UPDATE claims SET released_at = ?, outcome = ? WHERE issue_id = ? AND run_id = ?",
            (at.isoformat(), outcome, issue_id, run_id),
        )
        self.conn.commit()

    def open_claims(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM claims WHERE released_at IS NULL").fetchall()
        return [dict(row) for row in rows]

    # --- lusdetectie ---
    def role_run_count(self, issue_id: str, role: str, day: date) -> int:
        row = self.conn.execute(
            "SELECT count FROM role_runs WHERE issue_id = ? AND role = ? AND day = ?",
            (issue_id, role, day.isoformat()),
        ).fetchone()
        return int(row["count"]) if row else 0

    def bump_role_run(self, issue_id: str, role: str, day: date) -> None:
        self.conn.execute(
            "INSERT INTO role_runs (issue_id, role, day, count) VALUES (?,?,?,1) "
            "ON CONFLICT(issue_id, role, day) DO UPDATE SET count = count + 1",
            (issue_id, role, day.isoformat()),
        )
        self.conn.commit()

    # --- runs ---
    def add_run(self, run: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, issue_id, issue_identifier, team_key, rol, model, executor,"
            " klant, dienst, gestart, geeindigd, duur_s, beurten, tokens_in, tokens_uit, cache_lees,"
            " kosten_usd, kosten_eur, dod, uitkomst, volgende_status, pr_url, metered, artefacten)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run.run_id, run.issue_id, run.issue_identifier, run.team_key, run.rol, run.model, run.executor,
                run.klant, run.dienst, run.gestart.isoformat(),
                run.geeindigd.isoformat() if run.geeindigd else None,
                run.duur_s, run.beurten, run.tokens_in, run.tokens_uit, run.cache_lees,
                run.kosten_usd, run.kosten_eur, run.dod, run.uitkomst, run.volgende_status, run.pr_url,
                int(run.metered), json.dumps([a.__dict__ for a in run.artefacten]),
            ),
        )
        self.conn.commit()

    def runs_on(self, day: date) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM runs WHERE substr(gestart,1,10) = ?", (day.isoformat(),)
        ).fetchall()
        return [dict(row) for row in rows]

    # --- hartslag ---
    def last_heartbeat_at(self) -> Optional[datetime]:
        row = self.conn.execute("SELECT at FROM heartbeats ORDER BY at DESC LIMIT 1").fetchone()
        return datetime.fromisoformat(row["at"]) if row else None

    def record_heartbeat(self, *, at: datetime, cycle: int, comment_id, runs_today, cost_eur_today, queue_len) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO heartbeats VALUES (?,?,?,?,?,?)",
            (at.isoformat(), cycle, comment_id, runs_today, cost_eur_today, queue_len),
        )
        self.conn.commit()

    # --- MutationSink ---
    def record(self, m: Any) -> None:
        self.conn.execute(
            "INSERT INTO mutations (at, run_id, mutation, entity_id, variables_digest, variables_summary,"
            " result_id, ok, error, dry_run) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                m.at.astimezone(timezone.utc).isoformat(), m.run_id, m.mutation, m.entity_id,
                m.variables_digest, json.dumps(dict(m.variables_summary), default=str),
                m.result_id, int(m.ok), m.error, int(m.dry_run),
            ),
        )
        self.conn.commit()
