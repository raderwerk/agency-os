"""Configuratie: één keer lezen bij het opstarten, daarna bevroren.

Volgorde van winnen: expliciete overrides van de commandoregel > proceslocatie
`os.environ` > `~/.config/raderwerk/spil.env` > de standaardwaarde. Het bestand
is `KEY=VALUE` met `#`-commentaar, geen shellsyntaxis, en het wordt nooit
gecommit en nooit geprint.

`status` toont `linear_api_key_source` (bijvoorbeeld
`file:~/.config/raderwerk/spil.env`) en nooit de sleutel zelf: een geëxporteerde
sleutel die stilletjes het bestand verslaat is precies hoe een run op de
verkeerde werkplaats uitkomt.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Mapping, Optional

from agency_os.executors.base import ExecutorConfig
from agency_os.linear.ledger import FxRate, PriceRow

DEFAULT_ENV_FILE = Path("~/.config/raderwerk/spil.env")
DEFAULT_STATE_DIR = Path("~/.local/state/raderwerk")

# Lijstprijzen per miljoen tokens, in USD, op 2026-06-24. Dit is een schatting
# aan de klantkant op basis van lijstprijzen, geen factuurdata; het Kostenboek
# zegt dat er ook bij (spec hoofdstuk 11). De native lanes (Codex, Cursor)
# rekenen af binnen hun eigen abonnement en staan daarom op nul met
# `gemeten: false` in de staartregel, in plaats van met een verzonnen bedrag.
DEFAULT_PRICES: tuple[PriceRow, ...] = (
    PriceRow("claude-fable-5-1", 10.0, 50.0, 0.25),
    PriceRow("claude-opus-5", 5.0, 25.0, 0.50),
    PriceRow("claude-sonnet-5", 2.0, 10.0, 0.20),
    PriceRow("gpt-5.6-sol", 0.0, 0.0, 0.0),
    PriceRow("cursor-grok-4.6-high-fast", 0.0, 0.0, 0.0),
)

DEFAULTS: Mapping[str, str] = {
    "SPIL_LINEAR_ENDPOINT": "https://api.linear.app/graphql",
    "SPIL_PANEL_ISSUE": "WV-156",
    "SPIL_STATE_DIR": str(DEFAULT_STATE_DIR),
    "SPIL_INTERVAL_S": "60",
    "SPIL_MAX_CLAIMS_PER_CYCLE": "4",
    "SPIL_MAX_CONCURRENT_RUNS": "2",
    "SPIL_CLAIM_SETTLE_S": "5",
    "SPIL_RUN_TIMEOUT_S": "1800",
    "SPIL_NATIVE_SESSION_TIMEOUT_S": "3600",
    "SPIL_HEARTBEAT_EVERY_CYCLES": "15",
    "SPIL_WATCHDOG_MAX_AGE_S": "1800",
    "SPIL_ISSUE_BUDGET": "200,220,225",
    "SPIL_ALLOW_FABLE": "false",
    "SPIL_DRY_RUN": "false",
    "SPIL_CLAUDE_BIN": "claude",
    "SPIL_CODEX_BIN": "codex",
    "SPIL_GH_BIN": "gh",
    "SPIL_GIT_BIN": "git",
}

_TRUE = frozenset({"1", "true", "yes", "ja", "on"})


class ConfigError(RuntimeError):
    """De configuratie deugt niet. Altijd fataal, altijd met de naam erbij."""


@dataclass(frozen=True)
class Config:
    """Alles wat de Spil nodig heeft om te draaien, na validatie."""

    linear_api_key: str
    linear_api_key_source: str
    linear_endpoint: str
    dispatcher_user_id: str
    approver_ids: frozenset[str]
    panel_identifier: str
    state_dir: Path
    db_path: Path
    logbook_dir: Path
    interval_s: int
    max_claims_per_cycle: int
    max_concurrent_runs: int
    claim_settle_s: float
    heartbeat_every_cycles: int
    watchdog_max_age_s: int
    issue_budget: tuple[int, int, int]
    fx: FxRate
    prices: tuple[PriceRow, ...]
    allow_fable: bool
    dry_run: bool
    executors: ExecutorConfig

    @staticmethod
    def load(argv_overrides: Mapping[str, str] | None = None) -> "Config":
        """Leest env + bestand + standaarden en valideert alles wat fataal is."""
        return _load(argv_overrides or {})

    def redacted(self) -> dict:
        """Alles wat `status` mag tonen. De sleutel zit er niet in, de bron wel."""
        return {
            "linear_api_key_source": self.linear_api_key_source,
            "linear_endpoint": self.linear_endpoint,
            "dispatcher_user_id": self.dispatcher_user_id,
            "approvers": len(self.approver_ids),
            "panel_identifier": self.panel_identifier,
            "state_dir": str(self.state_dir),
            "db_path": str(self.db_path),
            "logbook_dir": str(self.logbook_dir),
            "interval_s": self.interval_s,
            "max_claims_per_cycle": self.max_claims_per_cycle,
            "max_concurrent_runs": self.max_concurrent_runs,
            "claim_settle_s": self.claim_settle_s,
            "heartbeat_every_cycles": self.heartbeat_every_cycles,
            "watchdog_max_age_s": self.watchdog_max_age_s,
            "issue_budget": list(self.issue_budget),
            "fx": {"usd_eur": self.fx.usd_eur, "source": self.fx.source, "on": self.fx.on.isoformat()},
            "prices": [p.model for p in self.prices],
            "allow_fable": self.allow_fable,
            "dry_run": self.dry_run,
            "repo_root": str(self.executors.repo_root),
            "worktree_root": str(self.executors.worktree_root),
        }

    def with_overrides(self, **changes) -> "Config":
        """Een kopie met een paar velden anders, voor `dry-run` en `--once`.

        `dry_run` trekt de uitvoerders mee: een droogloop waarin de executors
        toch echt draaien is geen droogloop.
        """
        cfg = replace(self, **changes)
        if "dry_run" in changes:
            cfg = replace(cfg, executors=replace(cfg.executors, dry_run=cfg.dry_run))
        return cfg


def read_env_file(path: Path) -> dict[str, str]:
    """`KEY=VALUE` per regel, `#` is commentaar. Ontbreekt het bestand: leeg."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _load(overrides: Mapping[str, str]) -> Config:
    env_file = Path(
        overrides.get("SPIL_CONFIG_FILE")
        or os.environ.get("SPIL_CONFIG_FILE")
        or str(DEFAULT_ENV_FILE)
    ).expanduser()
    from_file = read_env_file(env_file)

    def source(key: str) -> Optional[str]:
        if key in overrides:
            return "argv"
        if key in os.environ:
            return f"env:{key}"
        if key in from_file:
            return f"file:{env_file}"
        return None

    def get(key: str, default: Optional[str] = None) -> Optional[str]:
        for layer in (overrides, os.environ, from_file, DEFAULTS):
            if key in layer:
                return layer[key]
        return default

    def need(key: str) -> str:
        value = get(key)
        if not value:
            raise ConfigError(f"{key} ontbreekt (env, {env_file} of commandoregel)")
        return value

    api_key, api_key_source = _read_api_key(get, source, env_file)
    state_dir = Path(need("SPIL_STATE_DIR")).expanduser()
    repo_root = Path(get("SPIL_REPO_ROOT") or str(ExecutorConfig().repo_root)).expanduser()
    worktree_root = Path(get("SPIL_WORKTREE_ROOT") or str(repo_root / ".worktrees")).expanduser()
    dry_run = _as_bool(get("SPIL_DRY_RUN"))

    executors = ExecutorConfig(
        claude_bin=need("SPIL_CLAUDE_BIN"),
        codex_bin=need("SPIL_CODEX_BIN"),
        gh_bin=need("SPIL_GH_BIN"),
        git_bin=need("SPIL_GIT_BIN"),
        repo_root=repo_root,
        worktree_root=worktree_root,
        state_dir=state_dir,
        run_timeout_s=_as_int("SPIL_RUN_TIMEOUT_S", need("SPIL_RUN_TIMEOUT_S")),
        native_session_timeout_s=_as_int("SPIL_NATIVE_SESSION_TIMEOUT_S", need("SPIL_NATIVE_SESSION_TIMEOUT_S")),
        dry_run=dry_run,
    )
    for prefix in executors.forbidden_path_prefixes:
        if os.path.normcase(str(worktree_root.resolve())).startswith(os.path.normcase(prefix)):
            raise ConfigError(f"SPIL_WORKTREE_ROOT ligt onder een verboden pad: {prefix}")
    # De werkmapwortel is de zandbak waarin `--dangerously-skip-permissions` aan
    # gaat. Ligt hij buiten de klonen, dan verklaart een verkeerd gezette
    # variabele (`SPIL_WORKTREE_ROOT=$HOME`) de hele thuismap tot veilig gebied.
    resolved_root = repo_root.resolve()
    if not os.path.normcase(str(worktree_root.resolve())).startswith(
            os.path.normcase(str(resolved_root))):
        raise ConfigError(
            f"SPIL_WORKTREE_ROOT moet binnen SPIL_REPO_ROOT ({resolved_root}) liggen, "
            f"kreeg {worktree_root}")

    cfg = Config(
        linear_api_key=api_key,
        linear_api_key_source=api_key_source,
        linear_endpoint=need("SPIL_LINEAR_ENDPOINT"),
        dispatcher_user_id=need("SPIL_DISPATCHER_USER_ID"),
        approver_ids=_as_ids(need("SPIL_APPROVER_IDS")),
        panel_identifier=need("SPIL_PANEL_ISSUE"),
        state_dir=state_dir,
        db_path=state_dir / "spil.sqlite3",
        logbook_dir=state_dir / "logbook",
        interval_s=_as_int("SPIL_INTERVAL_S", need("SPIL_INTERVAL_S")),
        max_claims_per_cycle=_as_int("SPIL_MAX_CLAIMS_PER_CYCLE", need("SPIL_MAX_CLAIMS_PER_CYCLE")),
        max_concurrent_runs=_as_int("SPIL_MAX_CONCURRENT_RUNS", need("SPIL_MAX_CONCURRENT_RUNS")),
        claim_settle_s=_as_float("SPIL_CLAIM_SETTLE_S", need("SPIL_CLAIM_SETTLE_S")),
        heartbeat_every_cycles=_as_int("SPIL_HEARTBEAT_EVERY_CYCLES", need("SPIL_HEARTBEAT_EVERY_CYCLES")),
        watchdog_max_age_s=_as_int("SPIL_WATCHDOG_MAX_AGE_S", need("SPIL_WATCHDOG_MAX_AGE_S")),
        issue_budget=_as_budget(need("SPIL_ISSUE_BUDGET")),
        fx=_as_fx(need("SPIL_FX_USD_EUR"), need("SPIL_FX_SOURCE"), need("SPIL_FX_DATE")),
        prices=_as_prices(get("SPIL_PRICES")),
        allow_fable=_as_bool(get("SPIL_ALLOW_FABLE")),
        dry_run=dry_run,
        executors=executors,
    )
    if not cfg.approver_ids:
        raise ConfigError("SPIL_APPROVER_IDS is leeg: zonder goedkeurders kan geen poort open")
    return cfg


def _read_api_key(get, source, env_file: Path) -> tuple[str, str]:
    """De sleutel zelf, of de inhoud van het bestand waar `*_FILE` naar wijst.

    De sleutel staat al op deze machine in `~/.config/linear/api_key`. Hem ook
    in `spil.env` zetten maakt een tweede kopie van een levend geheim, en die
    twee lopen uit elkaar zodra er één geroteerd wordt. `SPIL_LINEAR_API_KEY`
    wint als hij gezet is; anders wordt het pad gelezen. De inhoud wordt nooit
    geprint, alleen de herkomst.
    """
    direct = get("SPIL_LINEAR_API_KEY")
    if direct:
        return direct, source("SPIL_LINEAR_API_KEY") or "onbekend"

    key_path = get("SPIL_LINEAR_API_KEY_FILE")
    if not key_path:
        raise ConfigError(
            "SPIL_LINEAR_API_KEY of SPIL_LINEAR_API_KEY_FILE ontbreekt "
            f"(env, {env_file} of commandoregel)"
        )
    path = Path(key_path).expanduser()
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"SPIL_LINEAR_API_KEY_FILE {path} is niet te lezen: {exc}") from exc
    if not value:
        raise ConfigError(f"SPIL_LINEAR_API_KEY_FILE {path} is leeg")
    return value, f"bestand:{path}"


def _as_bool(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in _TRUE


def _as_int(key: str, value: str) -> int:
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"{key} moet een geheel getal zijn, kreeg {value!r}") from exc


def _as_float(key: str, value: str) -> float:
    try:
        return float(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"{key} moet een getal zijn, kreeg {value!r}") from exc


def _as_ids(value: str) -> frozenset[str]:
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def _as_budget(value: str) -> tuple[int, int, int]:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(parts) != 3:
        raise ConfigError(
            f"SPIL_ISSUE_BUDGET moet drie getallen zijn (waarschuwen,beperken,stoppen), kreeg {value!r}"
        )
    warn, restrict, stop = (_as_int("SPIL_ISSUE_BUDGET", part) for part in parts)
    if not warn < restrict < stop:
        raise ConfigError(f"SPIL_ISSUE_BUDGET moet oplopen, kreeg {value!r}")
    return warn, restrict, stop


def _as_fx(rate: str, fx_source: str, on: str) -> FxRate:
    try:
        return FxRate(usd_eur=float(rate), source=fx_source, on=date.fromisoformat(on.strip()))
    except ValueError as exc:
        raise ConfigError(f"SPIL_FX_* is ongeldig ({rate!r}, {on!r}): {exc}") from exc


def _as_prices(value: Optional[str]) -> tuple[PriceRow, ...]:
    if not value:
        return DEFAULT_PRICES
    rows: list[PriceRow] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 4:
            raise ConfigError(f"SPIL_PRICES verwacht model:in:uit:cache, kreeg {item!r}")
        model, *numbers = parts
        try:
            in_price, out_price, cache_price = (float(n) for n in numbers)
        except ValueError as exc:
            raise ConfigError(f"SPIL_PRICES verwacht getallen in {item!r}") from exc
        rows.append(PriceRow(model, in_price, out_price, cache_price))
    return tuple(rows)
