"""De Claude-rolrunner: `claude -p --output-format json` in een eigen werkmap.

Zie docs/architecture.md sectie 10.1. De runner mergt niet, pusht niet naar
`main` en force-pusht niet: `gh.py` heeft geen merge-functie en
`worktree.push_branch` weigert de basisbranch.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Optional

from agency_os.executors.base import (
    ExecutionRequest,
    ExecutionResult,
    ExecutorConfig,
    ARTIFACT_TYPES,
    OUTCOMES,
    Artifact,
    ExecutorError,
    UnsafeWorktree,
    Usage,
    aborted,
    assert_safe_worktree,
    failed,
    utcnow,
    with_duration,
)
from agency_os.executors.process import ProcessResult, run_process, write_raw_log
from agency_os.executors.gh import open_pr
from agency_os.executors.worktree import (
    Worktree,
    ensure_worktree,
    has_commits_ahead,
    push_branch,
    repo_dir,
)

__all__ = ["ClaudeRunner", "RunResult", "parse_claude_json", "parse_runresult"]

SKIP_PERMISSIONS = "--dangerously-skip-permissions"


# --------------------------------------------------------------------------
# het RUNRESULT-contract (sectie 3.9); codex_cli leest dezelfde vorm
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


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _last_json_object(stdout: str) -> dict:
    """De laatste JSON-object in de uitvoer, hoeveel ruis er ook omheen staat."""
    text = (stdout or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        for item in reversed(data):
            if isinstance(item, dict) and ("result" in item or item.get("type") == "result"):
                return item
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                return candidate
    return {}


def parse_claude_json(stdout: str) -> tuple[str, Usage, Optional[str]]:
    """Lees resultaattekst, verbruik en sessie-id uit `--output-format json`.

    Elk veld is optioneel: ontbreekt het, dan wordt het nul en blijft de bron
    'unknown'. Een kapotte uitvoer levert nooit een exception op, want dan zou
    een fout van het model een fout van de Spil worden.
    """
    data = _last_json_object(stdout)
    usage_node = data.get("usage") if isinstance(data.get("usage"), dict) else {}

    tokens_in = _as_int(usage_node.get("input_tokens"))
    tokens_out = _as_int(usage_node.get("output_tokens"))
    cache_read = _as_int(usage_node.get("cache_read_input_tokens"))
    turns = _as_int(data.get("num_turns"))
    cost_usd = _as_float(data.get("total_cost_usd"))
    measured = any((tokens_in, tokens_out, cache_read, turns, cost_usd))

    usage = Usage(
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cache_read=cache_read,
        turns=turns,
        cost_usd=cost_usd,
        duration_s=_as_float(data.get("duration_ms")) / 1000.0,
        source="claude-json" if measured else "unknown",
        metered=measured,
    )
    session_id = str(data.get("session_id")) if data.get("session_id") else None
    result_text = str(data.get("result") or "")
    return result_text, usage, session_id


class ClaudeRunner:
    """Voert één rol uit met de Claude-CLI. Implementeert `SyncExecutor`."""

    name = "claude"

    def __init__(self, cfg: ExecutorConfig) -> None:
        self.cfg = cfg

    # -- uitvoering --------------------------------------------------------

    def run(self, req: ExecutionRequest) -> ExecutionResult:
        started_at = utcnow()
        try:
            worktree, cwd = self._workspace(req)
        except (ExecutorError, OSError) as exc:
            return failed(req, started_at, f"werkmap kon niet klaargezet worden: {exc}")

        cmd = [
            self.cfg.claude_bin,
            "-p",
            "--output-format",
            "json",
            "--model",
            req.model_key,
            *self._permission_flags(cwd, req.repo),
        ]
        if req.dry_run or self.cfg.dry_run:
            return failed(
                req,
                started_at,
                f"droogdraai: model niet aangeroepen ({' '.join(cmd)} in {cwd})",
                uitkomst="afgebroken",
                branch=worktree.branch if worktree else None,
            )

        try:
            proc = run_process(cmd, cwd=cwd, stdin_text=req.prompt, timeout_s=req.timeout_s)
        except OSError as exc:
            return failed(req, started_at, f"{self.cfg.claude_bin} niet uitvoerbaar: {exc}")

        raw_log = write_raw_log(self.cfg, req.run_id, proc.stdout, proc.stderr)
        if proc.timed_out:
            return replace(aborted(req, started_at, proc), raw_log_path=raw_log)

        result_text, usage, session_id = parse_claude_json(proc.stdout)
        usage = with_duration(usage, proc.duration_s)
        run_result = RunResult.from_dict(parse_runresult(result_text))
        error = self._error_for(run_result, proc)

        pr_url = run_result.pr_url
        if run_result.uitkomst == "klaar" and req.needs_pr:
            try:
                pr_url = self._publish(req, worktree) or pr_url
            except (ExecutorError, OSError) as exc:
                run_result = replace(run_result, uitkomst="mislukt")
                error = f"PR kon niet geopend worden: {exc}"

        return ExecutionResult(
            run_id=req.run_id,
            uitkomst=run_result.uitkomst,
            summary_md=run_result.samenvatting or (error or ""),
            dod=run_result.dod,
            question=run_result.vraag,
            error=error,
            pr_url=pr_url,
            branch=worktree.branch if worktree else None,
            artifacts=run_result.bewijs,
            usage=usage,
            started_at=started_at,
            ended_at=utcnow(),
            session_id=session_id,
            raw_log_path=raw_log,
        )

    # -- stappen -----------------------------------------------------------

    def _workspace(self, req: ExecutionRequest) -> tuple[Optional[Worktree], Path]:
        """De map waarin het model draait: een eigen werkmap, of de kloon zelf."""
        if not req.needs_worktree:
            root = repo_dir(self.cfg, req.repo) if req.repo else Path(self.cfg.repo_root)
            return None, root
        if not req.repo:
            raise ExecutorError("de rol vereist een werkmap, maar het issue noemt geen repo")
        worktree = ensure_worktree(
            self.cfg, req.repo, req.issue.identifier, req.issue.title, req.base_branch
        )
        return worktree, worktree.path

    def _permission_flags(self, cwd: Path, repo: Optional[str]) -> list[str]:
        """`--dangerously-skip-permissions` alleen binnen de gecontroleerde zandbak."""
        try:
            assert_safe_worktree(cwd, repo or "", self.cfg)
        except UnsafeWorktree:
            return []
        return [SKIP_PERMISSIONS]

    def _publish(self, req: ExecutionRequest, worktree: Optional[Worktree]) -> Optional[str]:
        """Push de branch en open (of hervind) de PR."""
        if worktree is None or not req.repo:
            raise ExecutorError("PR gevraagd zonder werkmap of repo")
        if not has_commits_ahead(self.cfg, worktree):
            raise ExecutorError(f"geen commits op {worktree.branch} ten opzichte van {worktree.base}")
        push_branch(self.cfg, worktree)
        pull_request = open_pr(
            self.cfg, req.repo, worktree.branch, worktree.base, req.pr_title, req.pr_body
        )
        return pull_request.url or None

    @staticmethod
    def _error_for(run_result: RunResult, proc: ProcessResult) -> Optional[str]:
        """Voeg de foutuitvoer toe als het model niets bruikbaars terugstuurde."""
        if run_result.uitkomst != "mislukt":
            return run_result.error
        tail = (proc.stderr or "").strip()[-500:]
        if proc.returncode != 0 and tail:
            return f"{run_result.error} (claude foutcode {proc.returncode}: {tail})"
        return run_result.error
