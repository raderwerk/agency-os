"""Hulpstukken voor de executor-tests: geen netwerk, geen Linear, wel echte git.

`tests/fakes.py` is van C en bestaat nog niet op deze basis. Dit bestand levert
alleen wat module B nodig heeft, in dezelfde vorm als het contract in
docs/architecture.md sectie 3.8, zodat de tests straks zonder aanpassing op C's
fakes over kunnen.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

from agency_os import gate
from agency_os.executors.base import ExecutionRequest, ExecutorConfig
from agency_os.executors.process import ProcessResult

NOW = datetime(2026, 9, 3, 9, 14, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# issue- en sessievormen (velden identiek aan linear.models)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeIssue:
    """De velden van `IssueView` die de executors aanraken."""

    id: str = "490eb350-0000-4000-8000-000000000207"
    identifier: str = "WV-207"
    title: str = "Publiek bouwlogboek, wekelijks"
    url: str = "https://linear.app/fightclub-techhub/issue/WV-207/publiek-bouwlogboek-wekelijks"
    team_key: str = "WV"
    state_name: str = "Ingepland"
    labels: tuple[str, ...] = ("dienst/content", "soort/contentstuk")
    risico: str = "laag"
    repo: Optional[str] = "raderwerk/raderwerk-content"
    delegate_id: Optional[str] = None

    @property
    def high_risk(self) -> bool:
        return self.risico == "hoog"


@dataclass(frozen=True)
class FakeActivity:
    """`ActivityView`-vorm."""

    id: str = "act-1"
    type: str = "response"
    body: str = ""
    created_at: datetime = NOW


@dataclass(frozen=True)
class FakeSession:
    """`AgentSessionView`-vorm."""

    id: str = "sessie-1"
    status: str = "active"
    summary: Optional[str] = None
    app_user_id: str = "app-codex"
    app_user_name: str = "Codex"
    created_at: datetime = NOW + timedelta(seconds=5)
    updated_at: datetime = NOW + timedelta(minutes=3)
    activities: tuple[FakeActivity, ...] = ()
    pull_request_url: Optional[str] = None


def make_issue(**overrides) -> FakeIssue:
    return replace(FakeIssue(), **overrides)


def make_session(**overrides) -> FakeSession:
    return replace(FakeSession(), **overrides)


def make_request(**overrides) -> ExecutionRequest:
    """Een WV-207-vormige aanvraag; overschrijf wat de test nodig heeft."""
    defaults = dict(
        run_id="3f9a2c",
        issue=make_issue(),
        role_key="redacteur",
        role_title="Redacteur",
        model_key="sonnet",
        model_display="Claude Sonnet 5",
        model_ledger="claude-sonnet-5",
        prompt="Doe het werk uit het issue.",
        repo="raderwerk/raderwerk-content",
        base_branch="main",
        branch="feat/WV-207-publiek-bouwlogboek",
        needs_worktree=False,
        needs_pr=False,
        pr_title="Publiek bouwlogboek",
        pr_body="Zie WV-207.",
        timeout_s=60,
        dry_run=False,
    )
    defaults.update(overrides)
    return ExecutionRequest(**defaults)


# --------------------------------------------------------------------------
# prijzen en koers (vorm van linear.ledger, die A later levert)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FakePriceRow:
    model: str
    usd_in_per_mtok: float
    usd_out_per_mtok: float
    usd_cache_read_per_mtok: float


@dataclass(frozen=True)
class FakeFxRate:
    usd_eur: float = 0.92
    source: str = "test"
    on: str = "2026-09-03"


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------


@dataclass
class FakeClient:
    """De leesbare kant van `LinearClient`, met dezelfde schrijfwachter op comments."""

    sessions: dict[str, list[FakeSession]] = field(default_factory=dict)
    comments: list[dict] = field(default_factory=list)
    updates: list[dict] = field(default_factory=list)
    next_comment_id: int = 1

    def agent_sessions(self, issue_id: str) -> list[FakeSession]:
        return list(self.sessions.get(issue_id, []))

    def create_comment(self, issue_id: str, body: str, *, run_id: str) -> str:
        gate.assert_not_gate_opening(body, author_is_agent=True)
        comment_id = f"comment-{self.next_comment_id}"
        self.next_comment_id += 1
        self.comments.append({"issue_id": issue_id, "body": body, "run_id": run_id})
        return comment_id

    def update_issue(self, issue_id: str, *, run_id: str, **kwargs) -> None:
        self.updates.append({"issue_id": issue_id, "run_id": run_id, **kwargs})


# --------------------------------------------------------------------------
# processen en configuratie
# --------------------------------------------------------------------------


def make_process(
    stdout: str = "", *, stderr: str = "", returncode: int = 0, timed_out: bool = False
) -> ProcessResult:
    return ProcessResult(
        cmd=("stub",),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_s=0.5,
    )


class RecordingProcess:
    """Vervangt `run_process` in een test en onthoudt elke aanroep."""

    def __init__(self, *results: ProcessResult) -> None:
        self.results = list(results) or [make_process()]
        self.calls: list[dict] = []

    def __call__(self, cmd: Sequence[str], **kwargs) -> ProcessResult:
        self.calls.append({"cmd": list(cmd), **kwargs})
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]

    @property
    def last_cmd(self) -> list[str]:
        return self.calls[-1]["cmd"]


def fake_config(root: Path, **overrides) -> ExecutorConfig:
    """Een configuratie die volledig binnen een tijdelijke map leeft."""
    defaults = dict(
        repo_root=root / "repos",
        worktree_root=root / "worktrees",
        state_dir=root / "state",
    )
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def git_sandbox(root: Path, repo: str = "raderwerk/agency-os") -> ExecutorConfig:
    """Bare origin plus kloon met één commit op main. Geen netwerk."""
    name = repo.split("/")[-1]
    origin = root / "origin" / f"{name}.git"
    clone = root / "repos" / name
    origin.parent.mkdir(parents=True, exist_ok=True)
    clone.parent.mkdir(parents=True, exist_ok=True)

    _git(None, "init", "--quiet", "--bare", "--initial-branch=main", str(origin))
    _git(None, "clone", "--quiet", str(origin), str(clone))
    for key, value in (
        ("user.email", "spil@raderwerk.invalid"),
        ("user.name", "Spil (test)"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ):
        _git(clone, "config", key, value)
    (clone / "README.md").write_text("raderwerk\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "--quiet", "-m", "init")
    _git(clone, "push", "--quiet", "--set-upstream", "origin", "main")
    return fake_config(root)


def commit_file(clone: Path, name: str, text: str) -> None:
    """Eén commit in een werkmap, zodat er iets te pushen valt."""
    (clone / name).write_text(text, encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "-c", "user.email=spil@raderwerk.invalid", "-c", "user.name=Spil (test)",
         "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", f"add {name}")


def _git(cwd: Optional[Path], *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
    )
