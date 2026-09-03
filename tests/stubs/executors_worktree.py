"""Stand-in voor `agency_os.executors.worktree` (onderdeel B), contract 3.6."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Worktree:
    repo: str
    path: Path
    branch: str
    base: str
    created: bool
    head_sha: str


def slugify(title: str, *, max_words: int = 4) -> str:
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return "-".join(words[:max_words])


def branch_name(identifier: str, title: str, *, prefix: str = "feat") -> str:
    return f"{prefix}/{identifier}-{slugify(title)}"
