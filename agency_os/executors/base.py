"""Contracten en gedeeld gereedschap voor alles wat buiten dit proces draait.

Zie docs/architecture.md sectie 3.5 en 3.6. Dit bestand bevat de bevroren
dataklassen waar module A en C tegenaan bouwen, de twee executor-protocollen,
de veiligheidscontrole op werkmappen en de vorm van een mislukte run. Het
starten van externe commando's staat in `process.py`, het RUNRESULT-blok in
`claude_runner.py` (de plek die sectie 3.6 ervoor aanwijst).

Deze module importeert nooit uit `agency_os.app` en uit A alleen
`agency_os.linear.models`. Schrijven naar Linear gebeurt uitsluitend via de
client die C meegeeft, nooit vanuit deze module zelf.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Protocol, Sequence

from agency_os.linear.models import Artifact


if TYPE_CHECKING:  # pragma: no cover - alleen voor typecontrole
    from agency_os.executors.process import ProcessResult
    from agency_os.linear.models import IssueView

__all__ = [
    "ARTIFACT_TYPES",
    "ALLOWED_ARTIFACT_HOSTS",
    "ALLOWED_REPOS",
    "OUTCOMES",
    "Artifact",
    "AsyncExecutor",
    "CommandFailed",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutorConfig",
    "ExecutorError",
    "SyncExecutor",
    "TriggerReceipt",
    "UnsafeWorktree",
    "Usage",
    "aborted",
    "assert_safe_worktree",
    "build_executors",
    "failed",
    "safe_artifacts",
    "utcnow",
    "with_duration",
]

#: De vier uitkomsten uit sectie 3.5; C leidt hier de volgende status uit af.
OUTCOMES = frozenset({"klaar", "vraag", "mislukt", "afgebroken"})

#: De vijf bewijstypen uit `Artifact.type`.
ARTIFACT_TYPES = frozenset({"pr", "preview", "document", "screenshot", "test"})

#: De enige hosts waar een bewijslink heen mag wijzen. Een model verzint de url
#: in zijn RUNRESULT-blok, en die url gaat rechtstreeks naar `attachmentLinkURL`
#: op een publiek issue: `javascript:`-links en vreemde domeinen horen daar niet.
ALLOWED_ARTIFACT_HOSTS = frozenset(
    {"github.com", "raderwerk.github.io", "linear.app"}
)

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
    #: De laan zelf kwam niet van de grond: geen werkmap, geen binair, een vlag
    #: die de CLI niet kent, een zandbak die weigert, een sessie die nooit
    #: startte. Er is dan geen model aan te pas gekomen en dus ook geen poging
    #: van de rol om te tellen. C streept zo'n run weg bij de lusdetectie; het
    #: Kostenboek houdt hem wel, want er ging tijd in zitten.
    infra_failure: bool = False


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
        # `os.path.normcase` omdat het bestandssysteem van deze machine niet naar
        # hoofdletters kijkt en `str.startswith` wel: zonder dit laat
        # `/users/youp/developer/fightclub/...` deze controle gewoon passeren.
        if _under(resolved, prefix) or _under(Path(path).expanduser(), prefix):
            raise UnsafeWorktree(f"werkmap {path} valt onder verboden pad {prefix}")


def safe_artifacts(
    artifacts: Sequence[Artifact],
) -> tuple[tuple[Artifact, ...], tuple[Artifact, ...]]:
    """Splitst bewijsstukken in (toegestaan, geweigerd) op schema en host.

    Alleen https en alleen de hosts uit `ALLOWED_ARTIFACT_HOSTS`. De geweigerde
    links verdwijnen niet stil: de aanroeper noemt ze in het comment.
    """
    from urllib.parse import urlparse

    kept: list[Artifact] = []
    refused: list[Artifact] = []
    for artifact in artifacts:
        parsed = urlparse(artifact.url or "")
        host = (parsed.hostname or "").lower()
        ok = parsed.scheme == "https" and host in ALLOWED_ARTIFACT_HOSTS
        (kept if ok else refused).append(artifact)
    return tuple(kept), tuple(refused)


def _under(path: Path, prefix: str) -> bool:
    """Ligt `path` onder `prefix`, ongeacht hoofdlettergebruik?"""
    return os.path.normcase(str(path)).startswith(os.path.normcase(prefix))


def failed(
    req: ExecutionRequest,
    started_at: datetime,
    error: str,
    *,
    uitkomst: str = "mislukt",
    usage: Optional[Usage] = None,
    branch: Optional[str] = None,
    infra_failure: bool = True,
) -> ExecutionResult:
    """Eén plek waar een mislukte of afgebroken run zijn vorm krijgt.

    `infra_failure` staat hier standaard aan: elke aanroep van deze functie is
    een laan die niet van de grond kwam. Draaide het model wél en kwam er alleen
    niets bruikbaars uit, dan bouwt de aanroeper zijn eigen `ExecutionResult`.
    """
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
        infra_failure=infra_failure,
    )


def aborted(
    req: ExecutionRequest, started_at: datetime, proc: "ProcessResult", *, source: str = "unknown"
) -> ExecutionResult:
    """De tijdslimiet verliep en de procesgroep is afgeschoten.

    Geen infrastructuurstoring: het model draaide, het draaide alleen te lang.
    Die run heeft echte beurten gekost en telt dus mee voor de lusdetectie.
    """
    return failed(
        req,
        started_at,
        f"tijdslimiet van {req.timeout_s}s overschreden; procesgroep afgeschoten",
        uitkomst="afgebroken",
        usage=Usage(duration_s=proc.duration_s, source=source, metered=False),
        infra_failure=False,
    )


def with_duration(usage: Usage, seconds: float) -> Usage:
    """Vul de duur aan als de executor er zelf geen meldde."""
    return usage if usage.duration_s else replace(usage, duration_s=seconds)
