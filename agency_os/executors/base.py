"""Contracten en gedeeld gereedschap voor alles wat buiten dit proces draait.

Zie docs/architecture.md sectie 3.5 en 3.6. Dit bestand bevat de bevroren
dataklassen waar module A en C tegenaan bouwen, de twee executor-protocollen,
de veiligheidscontrole op werkmappen, en de ene subprocess-aanroep die alle
executors delen (met tijdslimiet en procesgroep-kill).

Deze module importeert nooit uit `agency_os.app` en uit A alleen
`agency_os.linear.models`. Schrijven naar Linear gebeurt uitsluitend via de
client die C meegeeft, nooit vanuit deze module zelf.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Optional, Protocol, Sequence

try:  # pragma: no cover - A levert models.py in een eigen PR op dezelfde basis
    from agency_os.linear.models import Artifact
except ImportError:  # pragma: no cover - identiek aan het contract in sectie 3.2

    @dataclass(frozen=True)
    class Artifact:
        """Bewijsstuk bij een run. Zelfde velden als `linear.models.Artifact`."""

        type: str
        url: str
        label: str = ""


if TYPE_CHECKING:  # pragma: no cover - alleen voor typecontrole
    from agency_os.linear.models import IssueView

__all__ = [
    "ARTIFACT_TYPES",
    "ALLOWED_REPOS",
    "OUTCOMES",
    "Artifact",
    "AsyncExecutor",
    "CommandFailed",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutorConfig",
    "ExecutorError",
    "ProcessResult",
    "RunResult",
    "SyncExecutor",
    "TriggerReceipt",
    "UnsafeWorktree",
    "Usage",
    "assert_safe_worktree",
    "build_executors",
    "parse_runresult",
    "run_process",
    "utcnow",
]

#: De vier uitkomsten uit sectie 3.5; C leidt hier de volgende status uit af.
OUTCOMES = frozenset({"klaar", "vraag", "mislukt", "afgebroken"})

#: De vijf bewijstypen uit `Artifact.type`.
ARTIFACT_TYPES = frozenset({"pr", "preview", "document", "screenshot", "test"})

#: De acht publieke, fictieve repositories. Alles daarbuiten is verboden terrein.
ALLOWED_REPOS = frozenset(
    {
        "raderwerk/agency-os",
        "raderwerk/raderwerk-content",
        "raderwerk/raderwerk-site",
        "raderwerk/kantelbeer-site",
        "raderwerk/spoorlinde-web",
        "raderwerk/zoutkaap-shop",
        "raderwerk/zoutkaap-erp-bridge",
        "raderwerk/zoutkaap-erp-mock",
    }
)


class ExecutorError(RuntimeError):
    """Een executor kon zijn werk niet doen. Wordt vertaald naar uitkomst 'mislukt'."""


class UnsafeWorktree(ExecutorError):
    """De werkmap valt buiten de zandbak; er wordt niets uitgevoerd."""


class CommandFailed(ExecutorError):
    """Een extern commando gaf een foutcode terug."""

    def __init__(self, cmd: Sequence[str], returncode: int, stderr: str) -> None:
        self.cmd = tuple(cmd)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{cmd[0]} gaf foutcode {returncode}: {stderr.strip()[:400]}")


def utcnow() -> datetime:
    """Tijdzone-bewuste UTC-tijd; overal in dit pakket de enige klok."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# contracten
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    """Wat een run gekost heeft. `metered=False` betekent: niet te meten, geen schatting."""

    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    turns: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    source: str = "unknown"  # claude-json | codex-cli | native-unmetered | unknown
    metered: bool = True


def _default_repo_root() -> Path:
    return Path.home() / "Developer" / "Personal" / "Raderwerk"


def _default_worktree_root() -> Path:
    return _default_repo_root() / ".worktrees"


def _default_state_dir() -> Path:
    return Path.home() / ".local" / "state" / "raderwerk"


@dataclass(frozen=True)
class ExecutorConfig:
    """Alles wat de executors van de buitenwereld mogen weten. C bouwt dit."""

    claude_bin: str = "claude"
    codex_bin: str = "codex"
    gh_bin: str = "gh"
    git_bin: str = "git"
    repo_root: Path = field(default_factory=_default_repo_root)
    worktree_root: Path = field(default_factory=_default_worktree_root)
    state_dir: Path = field(default_factory=_default_state_dir)
    allowed_repos: frozenset[str] = ALLOWED_REPOS
    forbidden_path_prefixes: tuple[str, ...] = ("/Users/youp/Developer/Fightclub",)
    run_timeout_s: int = 1800
    native_session_timeout_s: int = 3600
    codex_model: str = "gpt-5.6-sol"
    codex_reasoning_effort: str = "xhigh"
    cursor_model: str = "cursor-grok-4.6-high-fast"
    dry_run: bool = False


@dataclass(frozen=True)
class ExecutionRequest:
    """Eén run van één rol op één issue. De prompt komt van C en blijft ongewijzigd."""

    run_id: str
    issue: "IssueView"
    role_key: str
    role_title: str
    model_key: str
    model_display: str
    model_ledger: str
    prompt: str
    repo: Optional[str]
    base_branch: str
    branch: str
    needs_worktree: bool
    needs_pr: bool
    pr_title: str
    pr_body: str
    timeout_s: int
    dry_run: bool


@dataclass(frozen=True)
class ExecutionResult:
    """Wat er van een run terugkomt. C maakt hier de comment en de statuswissel van."""

    run_id: str
    uitkomst: str
    summary_md: str
    dod: str
    question: Optional[str]
    error: Optional[str]
    pr_url: Optional[str]
    branch: Optional[str]
    artifacts: tuple[Artifact, ...]
    usage: Usage
    started_at: datetime
    ended_at: datetime
    session_id: Optional[str]
    raw_log_path: Optional[Path]


@dataclass(frozen=True)
class TriggerReceipt:
    """Bewijs dat een native agent aangestoten is, plus de strafpunten-teller."""

    run_id: str
    issue_id: str
    executor: str
    trigger_comment_id: Optional[str]
    session_id: Optional[str]
    triggered_at: datetime
    strikes: int


class SyncExecutor(Protocol):
    """Draait binnen de cyclus af en geeft meteen een resultaat."""

    name: str

    def run(self, req: ExecutionRequest) -> ExecutionResult: ...


class AsyncExecutor(Protocol):
    """Stoot iets aan dat elders draait; latere cycli pollen het."""

    name: str

    def trigger(self, client, req: ExecutionRequest) -> TriggerReceipt: ...

    def poll(
        self, client, receipt: TriggerReceipt, issue: "IssueView"
    ) -> tuple[TriggerReceipt, Optional[ExecutionResult]]: ...


def build_executors(cfg: ExecutorConfig) -> dict[str, "SyncExecutor | AsyncExecutor"]:
    """De vier lanen, op de naam waar `Route.executor_name` naar verwijst."""
    from agency_os.executors.claude_runner import ClaudeRunner
    from agency_os.executors.codex_cli import CodexCliReviewer
    from agency_os.executors.native import NativeExecutor

    return {
        "claude": ClaudeRunner(cfg),
        "codex-cli": CodexCliReviewer(cfg),
        "native-codex": NativeExecutor(cfg, "codex"),
        "native-cursor": NativeExecutor(cfg, "cursor"),
    }


# --------------------------------------------------------------------------
# veiligheid
# --------------------------------------------------------------------------


def assert_safe_worktree(path: Path, repo: str, cfg: ExecutorConfig) -> None:
    """Weiger elke werkmap die niet binnen de zandbak van de demo valt.

    Alle drie de voorwaarden moeten gelden: het opgeloste pad ligt onder
    `cfg.worktree_root`, de repository staat in `cfg.allowed_repos`, en het pad
    begint niet met een verboden prefix. `claude_runner` roept dit aan vlak
    voordat `--dangerously-skip-permissions` toegevoegd wordt, en voegt die vlag
    alleen toe als deze functie terugkeert.
    """
    if repo not in cfg.allowed_repos:
        raise UnsafeWorktree(f"repository {repo!r} staat niet in de toegestane lijst")

    resolved = Path(path).expanduser().resolve()
    root = Path(cfg.worktree_root).expanduser().resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise UnsafeWorktree(f"werkmap {resolved} ligt niet onder {root}")

    for prefix in cfg.forbidden_path_prefixes:
        if str(resolved).startswith(prefix) or str(Path(path)).startswith(prefix):
            raise UnsafeWorktree(f"werkmap {path} valt onder verboden pad {prefix}")


# --------------------------------------------------------------------------
# subprocessen
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessResult:
    """Uitkomst van één extern commando."""

    cmd: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def check(self) -> "ProcessResult":
        """Gooi `CommandFailed` als het commando niet slaagde."""
        if not self.ok:
            raise CommandFailed(self.cmd, self.returncode, self.stderr)
        return self


def _kill_group(proc: subprocess.Popen) -> None:
    """Ruim het hele procesgroep op: een model start zelf ook processen."""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):  # pragma: no cover - race bij afsluiten
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):  # pragma: no cover
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:  # pragma: no cover - dan volgt SIGKILL
            continue


def run_process(
    cmd: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    stdin_text: Optional[str] = None,
    timeout_s: Optional[float] = None,
    env: Optional[Mapping[str, str]] = None,
) -> ProcessResult:
    """Voer een commando uit in een eigen sessie, met tijdslimiet.

    Bij een tijdslimiet wordt de hele procesgroep afgeschoten (`start_new_session`
    plus `killpg`) — `subprocess.run` alleen doodt de kleinkinderen niet, en juist
    die houdt een vastgelopen modelrun in leven. `timed_out=True` vertaalt zich
    bij de aanroeper naar uitkomst 'afgebroken'.
    """
    started = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603 - vaste commando's uit de configuratie
        list(cmd),
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=dict(env) if env is not None else None,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(stdin_text, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError):  # pragma: no cover
            stdout, stderr = "", ""
    return ProcessResult(
        cmd=tuple(cmd),
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout or "",
        stderr=stderr or "",
        timed_out=timed_out,
        duration_s=time.monotonic() - started,
    )


# --------------------------------------------------------------------------
# het RUNRESULT-contract (sectie 3.9)
# --------------------------------------------------------------------------

_RUNRESULT_BLOCK = re.compile(
    r"```[ \t]*(?:json[ \t]+)?RUNRESULT[ \t]*\r?\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

NO_RUNRESULT = "geen RUNRESULT-blok"


def parse_runresult(result_text: str) -> dict:
    """Lees het laatste ```json RUNRESULT-blok. Ontbreekt of stuk: leeg dict.

    Dit is het enige wat uit modelproza gelezen wordt. Er wordt nooit iets uit
    de rest van de tekst geraden.
    """
    for raw in reversed(_RUNRESULT_BLOCK.findall(result_text or "")):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


@dataclass(frozen=True)
class RunResult:
    """Het gevalideerde RUNRESULT-blok, klaar om een `ExecutionResult` te vullen."""

    uitkomst: str
    samenvatting: str
    dod: str
    vraag: Optional[str]
    pr_url: Optional[str]
    bewijs: tuple[Artifact, ...]
    error: Optional[str]

    @staticmethod
    def from_dict(data: Mapping[str, object]) -> "RunResult":
        if not data:
            return RunResult("mislukt", "", "-", None, None, (), NO_RUNRESULT)

        uitkomst = str(data.get("uitkomst") or "").strip()
        samenvatting = str(data.get("samenvatting") or "").strip()
        vraag = _clean(data.get("vraag"))
        error: Optional[str] = None

        if uitkomst not in OUTCOMES:
            uitkomst, error = "mislukt", f"onbekende uitkomst in RUNRESULT: {uitkomst!r}"
        elif uitkomst == "vraag" and not vraag:
            uitkomst, error = "mislukt", "uitkomst 'vraag' zonder vraagtekst in RUNRESULT"
        elif uitkomst in ("mislukt", "afgebroken"):
            error = samenvatting or f"model meldde uitkomst {uitkomst!r} zonder toelichting"

        return RunResult(
            uitkomst=uitkomst,
            samenvatting=samenvatting,
            dod=str(data.get("dod") or "-").strip() or "-",
            vraag=vraag if uitkomst == "vraag" else None,
            pr_url=_clean(data.get("pr_url")),
            bewijs=_artifacts(data.get("bewijs")),
            error=error,
        )


def _clean(value: object) -> Optional[str]:
    text = str(value).strip() if value not in (None, "") else ""
    return text or None


def _artifacts(value: object) -> tuple[Artifact, ...]:
    """Bewijsstukken uit het RUNRESULT-blok; onbekende typen worden 'document'."""
    if not isinstance(value, list):
        return ()
    out: list[Artifact] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = _clean(item.get("url"))
        if not url:
            continue
        kind = str(item.get("type") or "").strip()
        out.append(
            Artifact(
                type=kind if kind in ARTIFACT_TYPES else "document",
                url=url,
                label=str(item.get("label") or "").strip(),
            )
        )
    return tuple(out)


def failed(
    req: ExecutionRequest,
    started_at: datetime,
    error: str,
    *,
    uitkomst: str = "mislukt",
    usage: Optional[Usage] = None,
    branch: Optional[str] = None,
) -> ExecutionResult:
    """Eén plek waar een mislukte of afgebroken run zijn vorm krijgt."""
    return ExecutionResult(
        run_id=req.run_id,
        uitkomst=uitkomst,
        summary_md=error,
        dod="-",
        question=None,
        error=error,
        pr_url=None,
        branch=branch or (req.branch if req.needs_worktree else None),
        artifacts=(),
        usage=usage or Usage(source="unknown", metered=False),
        started_at=started_at,
        ended_at=utcnow(),
        session_id=None,
        raw_log_path=None,
    )


def aborted(
    req: ExecutionRequest, started_at: datetime, proc: ProcessResult, *, source: str = "unknown"
) -> ExecutionResult:
    """De tijdslimiet verliep en de procesgroep is afgeschoten."""
    return failed(
        req,
        started_at,
        f"tijdslimiet van {req.timeout_s}s overschreden; procesgroep afgeschoten",
        uitkomst="afgebroken",
        usage=Usage(duration_s=proc.duration_s, source=source, metered=False),
    )


def with_duration(usage: Usage, seconds: float) -> Usage:
    """Vul de duur aan als de executor er zelf geen meldde."""
    return usage if usage.duration_s else replace(usage, duration_s=seconds)


def write_raw_log(
    cfg: ExecutorConfig, run_id: str, stdout: str, stderr: str
) -> Optional[Path]:
    """Ruwe uitvoer naar `<state_dir>/runs/<run_id>/`; die gaat nooit integraal naar Linear."""
    directory = Path(cfg.state_dir) / "runs" / run_id
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "stdout.json").write_text(stdout, encoding="utf-8")
        (directory / "stderr.txt").write_text(stderr, encoding="utf-8")
    except OSError:
        return None
    return directory
