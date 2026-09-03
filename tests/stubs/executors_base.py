"""Stand-in voor `agency_os.executors.base` (onderdeel B), contract 3.5."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Protocol

from agency_os.linear.models import Artifact


class UnsafeWorktree(RuntimeError):
    """Het pad, de repo of de prefix deugt niet."""


@dataclass(frozen=True)
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    turns: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    source: str = "unknown"
    metered: bool = True


@dataclass(frozen=True)
class ExecutorConfig:
    claude_bin: str = "claude"
    codex_bin: str = "codex"
    gh_bin: str = "gh"
    git_bin: str = "git"
    repo_root: Path = Path.home() / "Developer/Personal/Raderwerk"
    worktree_root: Path = Path.home() / "Developer/Personal/Raderwerk/.worktrees"
    allowed_repos: frozenset[str] = frozenset({
        "raderwerk/agency-os", "raderwerk/raderwerk-content", "raderwerk/raderwerk-site",
        "raderwerk/kantelbeer-site", "raderwerk/spoorlinde-web",
        "raderwerk/zoutkaap-shop", "raderwerk/zoutkaap-erp-bridge", "raderwerk/zoutkaap-erp-mock",
    })
    forbidden_path_prefixes: tuple[str, ...] = ("/Users/youp/Developer/Fightclub",)
    run_timeout_s: int = 1800
    native_session_timeout_s: int = 3600
    codex_model: str = "gpt-5.6-sol"
    codex_reasoning_effort: str = "xhigh"
    cursor_model: str = "cursor-grok-4.6-high-fast"
    dry_run: bool = False


@dataclass(frozen=True)
class ExecutionRequest:
    run_id: str
    issue: Any
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
    run_id: str
    issue_id: str
    executor: str
    trigger_comment_id: Optional[str]
    session_id: Optional[str]
    triggered_at: datetime
    strikes: int = 0


class SyncExecutor(Protocol):
    name: str

    def run(self, req: ExecutionRequest) -> ExecutionResult: ...


class AsyncExecutor(Protocol):
    name: str

    def trigger(self, client, req: ExecutionRequest) -> TriggerReceipt: ...

    def poll(self, client, receipt: TriggerReceipt, issue) -> tuple[TriggerReceipt, Optional[ExecutionResult]]: ...


def build_executors(cfg: ExecutorConfig) -> dict:
    return {}


def assert_safe_worktree(path: Path, repo: str, cfg: ExecutorConfig) -> None:
    resolved = Path(path).resolve()
    if repo not in cfg.allowed_repos:
        raise UnsafeWorktree(f"repo {repo} staat niet op de lijst")
    if not str(resolved).startswith(str(Path(cfg.worktree_root).resolve())):
        raise UnsafeWorktree(f"{resolved} ligt buiten {cfg.worktree_root}")
    for prefix in cfg.forbidden_path_prefixes:
        if str(resolved).startswith(prefix):
            raise UnsafeWorktree(f"{resolved} ligt onder een verboden pad")
