"""Git-werkmappen: aanmaken, hergebruiken, opruimen — en nooit naar de basis pushen.

Zie docs/architecture.md sectie 3.6, 10.1 en 13. Het hergebruik van een
bestaande `feat/<ISSUE>-*`-branch is de idempotentieproef uit sectie 8.2 van de
spec: een herstarte run maakt geen tweede branch en dus ook geen tweede PR.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from agency_os.executors.base import ExecutorConfig, ExecutorError, assert_safe_worktree
from agency_os.executors.process import ProcessResult, run_process

__all__ = [
    "PROTECTED_BRANCHES",
    "PushRefused",
    "Worktree",
    "branch_name",
    "ensure_detached_worktree",
    "ensure_worktree",
    "find_existing_branch",
    "has_commits_ahead",
    "push_branch",
    "remove_worktree",
    "repo_dir",
    "slugify",
]

#: Branches waar een agent nooit naartoe duwt, ook niet als een prompt erom vraagt.
PROTECTED_BRANCHES = frozenset({"main", "master", "production", "staging"})

_GIT_TIMEOUT_S = 300
_CUT = re.compile(r"[,;:·—–()/\[\]|]")
_WORD = re.compile(r"[a-z0-9]+")


class PushRefused(ExecutorError):
    """Er werd geprobeerd naar een basis- of beschermde branch te pushen."""


@dataclass(frozen=True)
class Worktree:
    """Een uitgecheckte werkmap voor één issue."""

    repo: str
    path: Path
    branch: str
    base: str
    created: bool
    head_sha: str


def repo_dir(cfg: ExecutorConfig, repo: str) -> Path:
    """De lokale kloon waar de werkmappen aan hangen."""
    return Path(cfg.repo_root) / repo.split("/")[-1]


def _worktree_path(cfg: ExecutorConfig, repo: str, identifier: str, suffix: str = "") -> Path:
    return Path(cfg.worktree_root) / repo.split("/")[-1] / f"{identifier}{suffix}"


def _git(cfg: ExecutorConfig, cwd: Path, *args: str) -> ProcessResult:
    return run_process([cfg.git_bin, *args], cwd=cwd, timeout_s=_GIT_TIMEOUT_S)


def slugify(title: str, *, max_words: int = 4) -> str:
    """'Publiek bouwlogboek, wekelijks' -> 'publiek-bouwlogboek'.

    De titel wordt eerst afgekapt op het eerste scheidingsteken (komma, dubbele
    punt, haakje), zodat de bijzin niet in de branchnaam belandt; daarna blijven
    hoogstens `max_words` woorden over.
    """
    head = _CUT.split(title or "", 1)[0]
    ascii_only = unicodedata.normalize("NFKD", head).encode("ascii", "ignore").decode()
    words = _WORD.findall(ascii_only.lower())
    return "-".join(words[:max_words])[:60].strip("-")


def branch_name(identifier: str, title: str, *, prefix: str = "feat") -> str:
    """`feat/WV-207-publiek-bouwlogboek`, conform AGENTS.md van elke repo."""
    return f"{prefix}/{identifier}-{slugify(title) or 'taak'}"


def _probe_branches(cfg: ExecutorConfig, repo: str, identifier: str) -> list[str]:
    """Bestaande `feat/<ISSUE>-*`-refs, lokaal vóór origin (alfabetisch gesorteerd)."""
    result = _git(
        cfg,
        repo_dir(cfg, repo),
        "for-each-ref",
        "--format=%(refname:short)",
        "--sort=refname",
        f"refs/heads/feat/{identifier}-*",
        f"refs/remotes/origin/feat/{identifier}-*",
    )
    if not result.ok:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def find_existing_branch(cfg: ExecutorConfig, repo: str, identifier: str) -> Optional[str]:
    """De branchnaam van een eerdere run op dit issue, of None."""
    for ref in _probe_branches(cfg, repo, identifier):
        return ref[len("origin/") :] if ref.startswith("origin/") else ref
    return None


def _fetch(cfg: ExecutorConfig, repo: str) -> None:
    """Best effort: zonder netwerk werkt de rest gewoon door op wat er lokaal is."""
    _git(cfg, repo_dir(cfg, repo), "fetch", "--prune", "origin")


def _head_sha(cfg: ExecutorConfig, path: Path) -> str:
    result = _git(cfg, path, "rev-parse", "HEAD")
    return result.stdout.strip() if result.ok else ""


def _current_branch(cfg: ExecutorConfig, path: Path) -> str:
    result = _git(cfg, path, "rev-parse", "--abbrev-ref", "HEAD")
    name = result.stdout.strip() if result.ok else ""
    return "" if name == "HEAD" else name


def _base_ref(cfg: ExecutorConfig, repo: str, base: str) -> str:
    """`origin/main` als dat bestaat, anders de lokale branch."""
    remote = f"origin/{base}"
    if _git(cfg, repo_dir(cfg, repo), "rev-parse", "--verify", "--quiet", remote).ok:
        return remote
    return base


def _add(cfg: ExecutorConfig, repo: str, path: Path, args: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(cfg, repo_dir(cfg, repo), "worktree", "add", *args).check()


def _require_clone(cfg: ExecutorConfig, repo: str) -> Path:
    directory = repo_dir(cfg, repo)
    if not (directory / ".git").exists():
        raise ExecutorError(f"geen lokale kloon van {repo} in {directory}")
    return directory


def ensure_worktree(
    cfg: ExecutorConfig, repo: str, identifier: str, title: str, base: str
) -> Worktree:
    """Lever een werkmap voor dit issue, met hergebruik van een eerdere branch.

    Bestaat de map al, dan wordt hij ongewijzigd teruggegeven (`created=False`):
    een herstart pakt het werk van de vorige run op in plaats van het te dubbelen.
    """
    path = _worktree_path(cfg, repo, identifier)
    assert_safe_worktree(path, repo, cfg)
    _require_clone(cfg, repo)

    if path.exists():
        return Worktree(
            repo=repo,
            path=path,
            branch=_current_branch(cfg, path) or branch_name(identifier, title),
            base=base,
            created=False,
            head_sha=_head_sha(cfg, path),
        )

    if cfg.dry_run:
        # Ook `git fetch` is een schrijfactie op de kloon van iemand anders; een
        # droogloop hoort geen enkele van de doelrepositories aan te raken.
        return Worktree(repo, path, branch_name(identifier, title), base, created=False,
                        head_sha="")

    _fetch(cfg, repo)
    refs = _probe_branches(cfg, repo, identifier)
    if refs and refs[0].startswith("origin/"):
        branch = refs[0][len("origin/") :]
        args = ["-b", branch, str(path), refs[0]]
    elif refs:
        branch = refs[0]
        args = [str(path), branch]
    else:
        branch = branch_name(identifier, title)
        args = ["-b", branch, str(path), _base_ref(cfg, repo, base)]

    _add(cfg, repo, path, args)
    return Worktree(repo, path, branch, base, created=True, head_sha=_head_sha(cfg, path))


def ensure_detached_worktree(
    cfg: ExecutorConfig, repo: str, identifier: str, ref: str, *, suffix: str = "-review"
) -> Worktree:
    """Een losse uitcheck op een commit, voor een reviewer die niets wijzigt.

    Zonder branch, dus `push_branch` weigert hem per definitie.
    """
    path = _worktree_path(cfg, repo, identifier, suffix)
    assert_safe_worktree(path, repo, cfg)
    _require_clone(cfg, repo)

    if path.exists():
        return Worktree(repo, path, "", ref, created=False, head_sha=_head_sha(cfg, path))
    if cfg.dry_run:
        return Worktree(repo, path, "", ref, created=False, head_sha="")

    _fetch(cfg, repo)
    _add(cfg, repo, path, ["--detach", str(path), ref])
    return Worktree(repo, path, "", ref, created=True, head_sha=_head_sha(cfg, path))


def has_commits_ahead(cfg: ExecutorConfig, wt: Worktree) -> bool:
    """Staat er iets nieuws op deze branch ten opzichte van de basis?"""
    base = _base_ref(cfg, wt.repo, wt.base)
    result = _git(cfg, wt.path, "rev-list", "--count", f"{base}..HEAD")
    if not result.ok:
        return False
    try:
        return int(result.stdout.strip() or "0") > 0
    except ValueError:  # pragma: no cover - git geeft altijd een getal
        return False


def push_branch(cfg: ExecutorConfig, wt: Worktree) -> None:
    """Duw de featurebranch naar origin. Naar de basisbranch pushen kan niet.

    Dit is geen prompt-afspraak maar een ontbrekende tak: de functie weigert het
    voordat er een commando gevormd wordt (docs/architecture.md sectie 10.1).
    """
    if not wt.branch:
        raise PushRefused("losse (detached) werkmap heeft geen branch om te pushen")
    if wt.branch == wt.base or wt.branch in PROTECTED_BRANCHES:
        raise PushRefused(f"push naar basisbranch {wt.branch!r} geweigerd")
    if cfg.dry_run:
        return
    _git(cfg, wt.path, "push", "--set-upstream", "origin", wt.branch).check()


def remove_worktree(cfg: ExecutorConfig, wt: Worktree, *, keep_branch: bool = True) -> None:
    """Ruim de werkmap op; de branch blijft standaard staan als bewijsmateriaal."""
    assert_safe_worktree(wt.path, wt.repo, cfg)
    if cfg.dry_run:
        return
    directory = repo_dir(cfg, wt.repo)
    _git(cfg, directory, "worktree", "remove", "--force", str(wt.path))
    if not keep_branch and wt.branch and wt.branch not in PROTECTED_BRANCHES:
        _git(cfg, directory, "branch", "-D", wt.branch)
    _git(cfg, directory, "worktree", "prune")
