"""Eén manier om iets buiten dit proces te starten, en het weer af te schieten.

Afgesplitst van `base.py` om onder de 400 regels per bestand te blijven
(docs/architecture.md sectie 2, "Size discipline"). Alle vier de lanen gebruiken
`run_process`: dat is de enige plek waar een tijdslimiet en een procesgroep-kill
staan, en dus de enige plek waar een vastgelopen model losgelaten wordt.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Optional, Sequence

from agency_os.executors.base import CommandFailed

if TYPE_CHECKING:  # pragma: no cover - alleen voor typecontrole
    from agency_os.executors.base import ExecutorConfig

__all__ = ["ProcessResult", "run_process", "write_raw_log"]


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
    proc = subprocess.Popen(
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


def write_raw_log(
    cfg: "ExecutorConfig", run_id: str, stdout: str, stderr: str
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
