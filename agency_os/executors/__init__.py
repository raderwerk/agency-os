"""Executors: alles wat de Spil buiten dit proces laat draaien.

Vier lanen, één contract (`docs/architecture.md` sectie 3.5 en 3.6):

* `claude` — de rolrunner, `claude -p --output-format json` in een eigen werkmap;
* `codex-cli` — de tweede reviewer, `codex exec` op een losse uitcheck;
* `native-codex` / `native-cursor` — een mention-comment plus het bewaken van de
  Agent Session, met de terugval na twee strafpunten.

Deze module schrijft nooit zelf naar Linear. De native lanen krijgen de client
van C mee en schrijven via de bewaakte methodes van module A, zodat elke mutatie
op één plek gelogd wordt.
"""

from agency_os.executors.base import (
    ALLOWED_REPOS,
    ARTIFACT_TYPES,
    Artifact,
    AsyncExecutor,
    CommandFailed,
    ExecutionRequest,
    ExecutionResult,
    ExecutorConfig,
    ExecutorError,
    OUTCOMES,
    SyncExecutor,
    TriggerReceipt,
    UnsafeWorktree,
    Usage,
    assert_safe_worktree,
    build_executors,
    utcnow,
)
from agency_os.executors.claude_runner import (
    ClaudeRunner,
    RunResult,
    parse_claude_json,
    parse_runresult,
)
from agency_os.executors.codex_cli import CodexCliReviewer
from agency_os.executors.cost import budget_flag, normalise, to_eur
from agency_os.executors.gh import PullRequest, find_pr_for_branch, open_pr, pr_diff, read_pr
from agency_os.executors.native import NativeExecutor, extract_pr_url, mention_body
from agency_os.executors.process import ProcessResult, run_process
from agency_os.executors.worktree import (
    PushRefused,
    Worktree,
    branch_name,
    ensure_worktree,
    find_existing_branch,
    push_branch,
    remove_worktree,
    slugify,
)

__all__ = [
    "ALLOWED_REPOS",
    "ARTIFACT_TYPES",
    "Artifact",
    "AsyncExecutor",
    "ClaudeRunner",
    "CodexCliReviewer",
    "CommandFailed",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutorConfig",
    "ExecutorError",
    "NativeExecutor",
    "OUTCOMES",
    "ProcessResult",
    "PullRequest",
    "PushRefused",
    "RunResult",
    "SyncExecutor",
    "TriggerReceipt",
    "UnsafeWorktree",
    "Usage",
    "Worktree",
    "assert_safe_worktree",
    "branch_name",
    "budget_flag",
    "build_executors",
    "ensure_worktree",
    "extract_pr_url",
    "find_existing_branch",
    "find_pr_for_branch",
    "mention_body",
    "normalise",
    "open_pr",
    "parse_claude_json",
    "parse_runresult",
    "pr_diff",
    "push_branch",
    "read_pr",
    "remove_worktree",
    "run_process",
    "slugify",
    "to_eur",
    "utcnow",
]
