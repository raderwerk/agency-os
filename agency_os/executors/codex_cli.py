"""De tweede reviewer: `codex exec` op een losse uitcheck van de PR-kop.

Zie docs/architecture.md sectie 10.3 en 11. Dit is de reviewer uit een andere
modelfamilie dan de uitvoerder — de goedkoopste kwaliteitsmaatregel die de
roster kent, en daarom verplicht zolang Fable uitstaat.

De codex-CLI meldt geen bedrag zoals `claude -p` dat doet. Staan er tokens in de
uitvoer, dan worden die geboekt met bron 'codex-cli'; anders gaat de regel als
ongemeten het Kostenboek in in plaats van stilzwijgend mee te middelen.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Optional

from agency_os.executors.base import (
    ExecutionRequest,
    ExecutionResult,
    ExecutorConfig,
    ExecutorError,
    RunResult,
    Usage,
    aborted,
    failed,
    parse_runresult,
    run_process,
    utcnow,
    with_duration,
    write_raw_log,
)
from agency_os.executors.gh import find_pr_for_branch, pr_diff
from agency_os.executors.worktree import (
    Worktree,
    ensure_detached_worktree,
    remove_worktree,
    repo_dir,
)

__all__ = ["CodexCliReviewer", "parse_codex_usage"]

_TOKENS = re.compile(
    r"tokens?\s*(?:used|usage)?\s*[:=]?.*?input[\s=:]+([\d,]+).*?output[\s=:]+([\d,]+)",
    re.IGNORECASE | re.DOTALL,
)
_CACHED = re.compile(r"cached[\s=:+]+([\d,]+)", re.IGNORECASE)


def _number(text: str) -> int:
    return int(text.replace(",", "").replace(".", "") or 0)


def parse_codex_usage(output: str) -> Usage:
    """Lees het tokenverbruik uit de codex-uitvoer; niet gevonden is ongemeten."""
    match = _TOKENS.search(output or "")
    if not match:
        return Usage(source="codex-cli", metered=False)
    cached = _CACHED.search(output or "")
    return Usage(
        tokens_in=_number(match.group(1)),
        tokens_out=_number(match.group(2)),
        cache_read=_number(cached.group(1)) if cached else 0,
        source="codex-cli",
        metered=True,
    )


class CodexCliReviewer:
    """Voert één reviewronde uit met de codex-CLI. Implementeert `SyncExecutor`."""

    name = "codex-cli"

    def __init__(self, cfg: ExecutorConfig) -> None:
        self.cfg = cfg

    def run(self, req: ExecutionRequest) -> ExecutionResult:
        started_at = utcnow()
        if not req.repo:
            return failed(req, started_at, "reviewronde zonder repo is niet te doen")

        pull_request = find_pr_for_branch(self.cfg, req.repo, req.branch)
        if pull_request is None:
            return failed(req, started_at, f"geen PR gevonden voor branch {req.branch}")

        try:
            diff = pr_diff(self.cfg, req.repo, pull_request.number)
        except (ExecutorError, OSError) as exc:
            return failed(req, started_at, f"diff van PR #{pull_request.number} niet te lezen: {exc}")

        worktree: Optional[Worktree] = None
        try:
            worktree = self._checkout(req, pull_request.head_sha)
            cwd = worktree.path if worktree else repo_dir(self.cfg, req.repo)
            prompt = self._prompt(req, pull_request.number, diff)

            if req.dry_run or self.cfg.dry_run:
                return failed(
                    req,
                    started_at,
                    f"droogdraai: codex niet aangeroepen voor PR #{pull_request.number}",
                    uitkomst="afgebroken",
                )

            proc = run_process(self._command(), cwd=cwd, stdin_text=prompt, timeout_s=req.timeout_s)
        except OSError as exc:
            return failed(req, started_at, f"{self.cfg.codex_bin} niet uitvoerbaar: {exc}")
        finally:
            if worktree is not None:
                self._cleanup(worktree)

        if proc.timed_out:
            return aborted(req, started_at, proc, source="codex-cli")

        run_result = RunResult.from_dict(parse_runresult(proc.stdout))
        if run_result.uitkomst == "mislukt" and proc.returncode != 0:
            run_result = replace(
                run_result,
                error=f"{run_result.error} (codex foutcode {proc.returncode}: "
                f"{proc.stderr.strip()[-400:]})",
            )
        usage = with_duration(parse_codex_usage(proc.stdout + proc.stderr), proc.duration_s)

        return ExecutionResult(
            run_id=req.run_id,
            uitkomst=run_result.uitkomst,
            summary_md=run_result.samenvatting or (run_result.error or ""),
            dod=run_result.dod,
            question=run_result.vraag,
            error=run_result.error,
            pr_url=run_result.pr_url or pull_request.url,
            branch=req.branch,
            artifacts=run_result.bewijs,
            usage=usage,
            started_at=started_at,
            ended_at=utcnow(),
            session_id=None,
            raw_log_path=write_raw_log(self.cfg, req.run_id, proc.stdout, proc.stderr),
        )

    # -- onderdelen --------------------------------------------------------

    def _command(self) -> list[str]:
        """De vaste aanroep uit sectie 10.3; de prompt komt via stdin ('-')."""
        return [
            self.cfg.codex_bin,
            "exec",
            "-m",
            self.cfg.codex_model,
            "-c",
            f"model_reasoning_effort={self.cfg.codex_reasoning_effort}",
            "-c",
            "notify=[]",
            "--search",
            "-",
        ]

    def _checkout(self, req: ExecutionRequest, head_sha: str) -> Optional[Worktree]:
        """Een losse uitcheck op de PR-kop; mislukt dat, dan volstaat de kloon."""
        if not head_sha or not req.repo:
            return None
        try:
            return ensure_detached_worktree(
                self.cfg, req.repo, req.issue.identifier, head_sha
            )
        except (ExecutorError, OSError):
            return None

    def _cleanup(self, worktree: Worktree) -> None:
        try:
            remove_worktree(self.cfg, worktree)
        except (ExecutorError, OSError):  # pragma: no cover - opruimen mag nooit de run breken
            pass

    @staticmethod
    def _prompt(req: ExecutionRequest, number: int, diff: str) -> str:
        """De prompt van C, met de diff eronder. De prompt zelf blijft ongewijzigd."""
        return f"{req.prompt}\n\n## Diff van PR #{number}\n\n```diff\n{diff}\n```\n"
