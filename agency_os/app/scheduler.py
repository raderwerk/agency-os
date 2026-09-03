"""De cyclus: lezen, schakelaars, poorten, claimen, routeren, draaien, terugschrijven.

Eén cyclus per interval, precies één dispatcherproces, en de volgorde van spec
8.1: poortbeslissingen gaan vóór nieuw werk, want een mens die heeft geantwoord
wacht al langer dan een issue dat nog niet begonnen is.

Van de Store gebruikt deze module, naast wat `routing` en `heartbeat` al nodig
hebben: `record(m)` (de Store is zelf een MutationSink), `set_meta(key, value)`,
`get_meta(key, default)` en `bump_role_run(issue_id, role, day)`.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Optional, Sequence

from agency_os.app import heartbeat, runs
from agency_os.app.config import Config
from agency_os.app.logbook import Logbook
from agency_os.app.routing import Refusal, Route, RoutingTable, decide, load_table, loop_guard
from agency_os.app.runs import Job
from agency_os.executors import base as executors
from agency_os.linear import claim as claims
from agency_os.linear import comments, gates, killswitch, machine
from agency_os.linear import poll as polling
from agency_os.linear.client import LinearClient, LinearError, WriteRefused
from agency_os.linear.poll import PollConfig
from agency_os.linear.store import Store

IN_SCOPE_STATES: Mapping[str, tuple[str, ...]] = {
    "WV": ("Ingepland", "Agentreview", "QA op preview", "Na-merge controle"),
    "KR": ("Lead", "Discovery", "Voorstel"),
}
BLOCKING_LABELS = ("run/bezet", "schakelaar/pauze", "run/vastgelopen", "run/onbevestigd",
                   "schakelaar/mens-vereist", "agent/mens")
#: Bij een poort telt `run/bezet` niet mee: dat label is daar het spoor van een
#: gestrande run, en dat mag een menselijk besluit niet tegenhouden.
GATE_BLOCKING_LABELS = tuple(label for label in BLOCKING_LABELS if label != "run/bezet")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CycleReport:
    """Wat er in één cyclus is gebeurd. Gaat naar het logboek en naar de CLI."""

    at: datetime
    cycle: int
    polled: int
    claimed: int
    finished: int
    gates_seen: int
    gates_applied: int
    halted: bool
    errors: tuple[str, ...]
    # Wat er geclaimd en waarheen gerouteerd is, als leesbare regels. Zonder dit
    # laat `dry-run` alleen de mutaties zien en blijft de routering onzichtbaar,
    # terwijl dat juist het antwoord is waarvoor je een droogloop draait.
    routed: tuple[str, ...] = ()


@dataclass
class Context:
    """Alles wat een cyclus nodig heeft. Eén keer gebouwd, daarna doorgegeven."""

    cfg: Config
    client: Any
    store: Any
    table: RoutingTable
    logbook: Logbook
    executors: Mapping[str, Any]
    poll_cfg: Any
    only_issue: Optional[str] = None
    now: Callable[[], datetime] = _utcnow
    failures: MutableMapping[tuple[str, str], int] = field(default_factory=dict)
    receipts: MutableMapping[str, Any] = field(default_factory=dict)


def build_context(
    cfg: Config, *, only_issue: Optional[str] = None, extra_sinks: Sequence[Any] = ()
) -> Context:
    """Bouwt de client, de store, het logboek en de uitvoerders uit de config."""
    logbook = Logbook(_logbook_dir(cfg))
    store = Store(_db_path(cfg))
    client = LinearClient(
        cfg.linear_api_key,
        endpoint=cfg.linear_endpoint,
        dispatcher_user_id=cfg.dispatcher_user_id,
        sinks=(store, logbook, *extra_sinks),
        dry_run=cfg.dry_run,
    )
    return Context(
        cfg=cfg,
        client=client,
        store=store,
        table=load_table(),
        logbook=logbook,
        executors=executors.build_executors(cfg.executors),
        poll_cfg=PollConfig(
            team_keys=tuple(IN_SCOPE_STATES),
            panel_identifier=cfg.panel_identifier,
            in_scope_states=IN_SCOPE_STATES,
            max_claims=cfg.max_claims_per_cycle,
            issue_budget=cfg.issue_budget,
        ),
        only_issue=only_issue,
    )


def _db_path(cfg: Config) -> Path:
    """Een droogloop krijgt zijn eigen sqlite, en raakt de echte nooit aan.

    `dry_run` dempt de schrijfacties naar Linear, maar `bump_role_run`,
    `ledger.record_run` en `insert_claim` schrijven gewoon door. Op de echte
    database betekent dat: de droogloop uit het leesmij-recept laat vier open
    claims achter en laat de lusdetectie de eerstvolgende échte run weigeren.
    """
    return Path(cfg.state_dir) / "dry-run.sqlite3" if cfg.dry_run else Path(cfg.db_path)


def _logbook_dir(cfg: Config) -> Path:
    return Path(cfg.logbook_dir) / "dry-run" if cfg.dry_run else Path(cfg.logbook_dir)


def run_cycle(ctx: Context, cycle_index: int) -> CycleReport:
    """Eén volledige cyclus."""
    at = ctx.now()
    errors: list[str] = []
    if cycle_index == 1 and not ctx.cfg.dry_run:
        # Verzoening bij het opstarten (architectuur 6.1 en 13): een proces dat
        # omviel laat zijn claimrij open staan, en die rij houdt het issue
        # voorgoed onclaimbaar. `status` en `ledger` bouwen dezelfde context en
        # blijven leesbewerkingen, dus dit hoort in de cyclus en niet daarvoor.
        reclaimed = ctx.store.release_stale_claims(
            ctx.now() - timedelta(seconds=ctx.cfg.executors.run_timeout_s))
        if reclaimed:
            ctx.logbook.write("halt", run_id=None, issue=None,
                              payload={"verweesde_claims_vrijgegeven": reclaimed})
    result = polling.poll(ctx.client, ctx.poll_cfg)
    ctx.store.set_meta("cycle", str(cycle_index))
    ctx.store.set_meta("queue_len", str(len(result.ready)))
    # De hartslag en de dagafsluiting lezen deze teller terug; zonder deze regel
    # melden ze allebei eeuwig "Issueteller: 0".
    ctx.store.set_meta("issue_count", str(result.switches.issue_count))
    ctx.logbook.write(
        "poll",
        run_id=None,
        issue=None,
        payload={
            "cycle": cycle_index,
            "ready": [i.identifier for i in result.ready],
            "gates": [i.identifier for i in result.gates],
            "watching": [i.identifier for i in result.watching],
            "budget": result.switches.budget_level,
        },
    )
    for identifier, reason in result.skipped:
        ctx.logbook.write("skip", run_id=None, issue=identifier, payload={"reden": reason})

    if result.switches.global_pause or result.switches.budget_level == "stop":
        return _halt(ctx, result, cycle_index, at)

    if result.switches.budget_level == "warn":
        _warn_once(ctx, result)

    gates_seen, gates_applied = _apply_gates(ctx, result, errors)
    watching = {issue.id: issue for issue in result.watching}
    finished = runs.collect_native(ctx, watching, errors, guard=_guard)
    _reconcile(ctx, result, errors)
    jobs = _claim(ctx, result, errors)
    finished += runs.execute(ctx, jobs, errors, guard=_guard)

    if heartbeat.due(cycle_index, ctx.cfg.heartbeat_every_cycles) and result.panel is not None:
        _guard(errors, "hartslag", lambda: heartbeat.beat(
            ctx.client, ctx.store, ctx.cfg, result.panel, run_id=secrets.token_hex(3)))

    report = CycleReport(
        at=at,
        cycle=cycle_index,
        polled=len(result.ready) + len(result.gates) + len(result.watching),
        claimed=len(jobs),
        finished=finished,
        gates_seen=gates_seen,
        gates_applied=gates_applied,
        halted=False,
        errors=tuple(errors),
        routed=tuple(
            f"{job.issue.identifier} -> {job.route.role.key} / {job.route.model_key} "
            f"({job.route.executor_name})"
            for job in jobs
        ),
    )
    ctx.logbook.write("poll", run_id=None, issue=None, payload={"cyclus_verslag": _as_dict(report)})
    return report


def run_loop(ctx: Context, interval_s: int, *, stop: threading.Event) -> None:
    """Draait cycli tot `stop` gezet is. Cycli overlappen nooit."""
    cycle = 0
    while not stop.is_set():
        cycle += 1
        started = time.monotonic()
        try:
            run_cycle(ctx, cycle)
        except Exception as exc:  # één kapotte cyclus mag de dispatcher niet doden
            ctx.logbook.write("error", run_id=None, issue=None, payload={"cyclus": cycle, "fout": str(exc)})
        elapsed = time.monotonic() - started
        if elapsed > interval_s:
            ctx.logbook.write("poll", run_id=None, issue=None,
                          payload={"cyclus": cycle, "overschrijding_s": round(elapsed - interval_s, 1)})
        stop.wait(max(0.0, interval_s - elapsed))


# ---------- noodrem en budget ----------


def _halt(ctx: Context, result: Any, cycle_index: int, at: datetime) -> CycleReport:
    """Alles stilzetten. Er gebeurt in deze cyclus verder niets meer."""
    run_id = secrets.token_hex(3)
    if result.switches.budget_level == "stop" and result.panel is not None:
        killswitch.trip_emergency_stop(
            ctx.client, result.panel, result.switches.reason or "issuebudget op", run_id=run_id
        )
    aborted = killswitch.halt_everything(ctx.client, ctx.store, result.switches, run_id=run_id)
    ctx.logbook.write("halt", run_id=run_id, issue=None,
                      payload={"afgebroken": aborted, "reden": result.switches.reason})
    return CycleReport(at, cycle_index, 0, 0, 0, 0, 0, True, ())


def _warn_once(ctx: Context, result: Any) -> None:
    """Bij budgetniveau 'waarschuwen' hooguit één paneelcomment per dag."""
    today = ctx.now().date().isoformat()
    if result.panel is None or ctx.store.get_meta("budget_warn_day") == today:
        return
    run_id = secrets.token_hex(3)
    ctx.client.create_comment(
        result.panel.id,
        comments.signature("Spil", "dispatcher", run_id, ctx.now())
        + f"\n\nIssueteller staat op {result.switches.issue_count}. "
        f"Bij {ctx.cfg.issue_budget[1]} claim ik alleen nog incidenten, "
        f"bij {ctx.cfg.issue_budget[2]} zet ik de noodstop.",
        run_id=run_id,
    )
    ctx.store.set_meta("budget_warn_day", today)


# ---------- poorten ----------


def _apply_gates(ctx: Context, result: Any, errors: list[str]) -> tuple[int, int]:
    """Poortbeslissingen eerst. Ongeldig is nooit 'toch maar door'."""
    seen = applied = 0
    for issue in result.gates:
        if ctx.only_issue and issue.identifier != ctx.only_issue:
            continue
        blocker = _blocked(issue, result.switches, labels=GATE_BLOCKING_LABELS)
        if blocker:
            # Een poortstatus is geen uitzondering op de rem. `poll` zet elk
            # `Poort*`-issue in `gates` vóór de labelcontrole, dus zonder deze
            # regel wordt een gepauzeerd of al onbevestigd issue elke ronde
            # opnieuw beoordeeld en beschreven.
            ctx.logbook.write("skip", run_id=None, issue=issue.identifier,
                              payload={"reden": blocker, "waar": "poort"})
            continue
        seen += 1
        run_id = secrets.token_hex(3)
        try:
            obs = gates.evaluate_gate(
                ctx.client, issue,
                approver_ids=ctx.cfg.approver_ids, dispatcher_user_id=ctx.cfg.dispatcher_user_id,
            )
            ctx.logbook.write(
                "gate",
                run_id=run_id,
                issue=issue.identifier,
                payload={"outcome": obs.outcome, "valid": obs.valid, "refusal": obs.refusal, "source": obs.source},
            )
            if obs.outcome is None:
                continue
            if not obs.valid:
                gates.mark_unconfirmed(ctx.client, ctx.store, issue, obs, run_id=run_id)
                continue
            gates.apply_gate_decision(ctx.client, ctx.store, issue, obs, run_id=run_id)
            applied += 1
        except (LinearError, WriteRefused) as exc:
            errors.append(f"poort {issue.identifier}: {exc}")
    return seen, applied


def _reconcile(ctx: Context, result: Any, errors: list[str]) -> int:
    """`run/bezet` zonder open claim is een gestrande run, geen lopende.

    Zonder deze stap houdt één gevallen run het issue voorgoed in `watching`:
    de poll zet het nooit meer in `ready`, dus het wordt nooit meer geclaimd en
    nooit meer vrijgegeven. De verweesde rijen in sqlite gaan bij de eerste
    cyclus open (architectuur 6.1 en 13); de labels die erbij horen gaan hier.
    """
    freed = 0
    for issue in result.watching:
        if issue.id in ctx.receipts or ctx.store.open_claim(issue.id) is not None:
            continue
        if ctx.only_issue and issue.identifier != ctx.only_issue:
            continue
        run_id = secrets.token_hex(3)
        try:
            ctx.client.update_issue(issue.id, run_id=run_id,
                                    added_labels=[claims.QUEUE_LABEL],
                                    removed_labels=[claims.BUSY_LABEL])
        except (LinearError, WriteRefused) as exc:
            errors.append(f"verzoening {issue.identifier}: {exc}")
            continue
        ctx.logbook.write("skip", run_id=run_id, issue=issue.identifier,
                          payload={"reden": "verweesde run/bezet teruggezet op run/wachtrij"})
        freed += 1
    return freed


# ---------- claimen en routeren ----------


def _claim(ctx: Context, result: Any, errors: list[str]) -> list[Job]:
    """Claimt maximaal `max_claims_per_cycle` issues, in de volgorde van de poll."""
    jobs: list[Job] = []
    day = ctx.now().date()
    for issue in result.ready:
        if len(jobs) >= ctx.cfg.max_claims_per_cycle:
            break
        if ctx.only_issue and issue.identifier != ctx.only_issue:
            continue
        blocker = _blocked(issue, result.switches)
        if blocker:
            ctx.logbook.write("skip", run_id=None, issue=issue.identifier, payload={"reden": blocker})
            continue

        outcome = decide(ctx.table, issue, allow_fable=ctx.cfg.allow_fable)
        if isinstance(outcome, Refusal):
            _refuse(ctx, issue, outcome, errors)
            continue
        stop_reason = loop_guard(ctx.store, issue, outcome.role.key, day)
        if stop_reason:
            _stop_loop(ctx, issue, stop_reason, errors)
            continue

        run_id = secrets.token_hex(3)
        try:
            claim = claims.try_claim(ctx.client, ctx.store, issue, run_id, settle_s=ctx.cfg.claim_settle_s)
        except (LinearError, WriteRefused) as exc:
            errors.append(f"claim {issue.identifier}: {exc}")
            continue
        if claim is None:
            ctx.logbook.write("skip", run_id=run_id, issue=issue.identifier,
                              payload={"reden": "claim niet gekregen"})
            continue

        ctx.store.bump_role_run(issue.id, outcome.role.key, day)
        state = _start_state(issue, outcome)
        if state != issue.state_name:
            ctx.client.update_issue(issue.id, run_id=run_id, state=state)
        ctx.logbook.write(
            "route",
            run_id=run_id,
            issue=issue.identifier,
            payload={"rol": outcome.role.key, "model": outcome.model_key,
                     "uitvoerder": outcome.executor_name, "reden": outcome.reason},
        )
        jobs.append(Job(run_id=run_id, issue=issue, route=outcome, claim=claim, state=state))
    return jobs


def _blocked(issue: Any, switches: Any,
             labels: Sequence[str] = BLOCKING_LABELS) -> Optional[str]:
    """De redenen om een issue in deze cyclus met rust te laten."""
    for label in labels:
        if label in issue.labels:
            return label
    if issue.id in switches.paused_issue_ids:
        return "schakelaar/pauze"
    if switches.budget_level == "restrict" and issue.soort != "incident":
        return "issuebudget: alleen incidenten"
    return None


def _start_state(issue: Any, route: Route) -> str:
    """De status waarin het issue staat terwijl de run loopt.

    Alleen verplaatsen als de machinetabel dezelfde stap bedoelt als de rol
    (WV Ingepland -> In uitvoering). Op de andere statussen blijft het issue
    staan waar het staat; de statustabel is de enige die statussen bepaalt.
    """
    planned = machine.next_state(issue.team_key, issue.state_name, "klaar")
    return route.role.working_state if planned == route.role.working_state else issue.state_name


def _refuse(ctx: Context, issue: Any, refusal: Refusal, errors: list[str]) -> None:
    """Een geweigerd issue krijgt een reden te horen, geen stilte."""
    ctx.logbook.write("skip", run_id=None, issue=issue.identifier,
                      payload={"reden": refusal.code, "toelichting": refusal.reason})
    if refusal.action == "overslaan":
        return
    run_id = secrets.token_hex(3)
    body = comments.signature("Spil", "dispatcher", run_id, ctx.now()) + f"\n\n{refusal.reason}."
    try:
        if refusal.action == "backlog":
            ctx.client.create_comment(issue.id, body + " Ik zet het terug op Backlog.", run_id=run_id)
            ctx.client.update_issue(issue.id, run_id=run_id, state="Backlog")
        else:
            ctx.client.create_comment(issue.id, body + " Ik heb dit nodig voordat ik verder kan.", run_id=run_id)
            ctx.client.update_issue(
                issue.id,
                run_id=run_id,
                state=machine.WAIT_STATE[issue.team_key],
                added_labels=["schakelaar/mens-vereist"],
                assignee_id=sorted(ctx.cfg.approver_ids)[0],
            )
    except (LinearError, WriteRefused) as exc:
        errors.append(f"weigering {issue.identifier}: {exc}")


def _stop_loop(ctx: Context, issue: Any, reason: str, errors: list[str]) -> None:
    """Lusdetectie: label, pauze op dit issue, één comment, geen claim."""
    run_id = secrets.token_hex(3)
    ctx.logbook.write("skip", run_id=run_id, issue=issue.identifier,
                      payload={"reden": "lus-verdacht", "toelichting": reason})
    try:
        ctx.client.create_comment(
            issue.id, comments.signature("Spil", "dispatcher", run_id, ctx.now()) + f"\n\n{reason}.", run_id=run_id
        )
        ctx.client.update_issue(issue.id, run_id=run_id, added_labels=["lus-verdacht", "schakelaar/pauze"])
    except (LinearError, WriteRefused) as exc:
        errors.append(f"lusdetectie {issue.identifier}: {exc}")


def _guard(errors: list[str], what: str, action: Callable[[], Any]) -> None:
    """Voert iets uit en bewaart de fout in plaats van de cyclus te laten vallen."""
    try:
        action()
    except Exception as exc:
        errors.append(f"{what}: {exc}")


def _as_dict(report: CycleReport) -> dict:
    return {
        "cyclus": report.cycle,
        "gepolld": report.polled,
        "geclaimd": report.claimed,
        "afgerond": report.finished,
        "poorten_gezien": report.gates_seen,
        "poorten_toegepast": report.gates_applied,
        "gestopt": report.halted,
        "gerouteerd": list(report.routed),
        "fouten": list(report.errors),
    }
