"""De commandoregel: zes subcommando's, vier afloopcodes.

    python -m agency_os run --once [--issue WV-207] [--max-claims N]
    python -m agency_os run --loop --interval 60
    python -m agency_os status [--json]
    python -m agency_os dry-run [--issue WV-207] [--cycles 1]
    python -m agency_os heartbeat [--watchdog]
    python -m agency_os ledger [--since D] [--until D] [--format markdown|json] [--logbook]

Afloopcodes: 0 in orde, 1 ongezond of geweigerd, 2 configuratiefout, 130 onderbroken.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import signal
import threading
from datetime import date, datetime, timezone
from typing import Any, Optional, Sequence

from agency_os.app import heartbeat as heartbeat_mod
from agency_os.app import scheduler
from agency_os.app.config import Config, ConfigError
from agency_os.app.logbook import Logbook
from agency_os.app.routing import RoutingError

OK, UNHEALTHY, CONFIG_ERROR, INTERRUPTED = 0, 1, 2, 130


def main(argv: Sequence[str] | None = None) -> int:
    """Leest de argumenten, laadt de config en voert het subcommando uit."""
    args = _parser().parse_args(argv)
    try:
        cfg = Config.load(_overrides(args))
    except (ConfigError, RoutingError) as exc:
        print(f"configuratiefout: {exc}")
        return CONFIG_ERROR
    try:
        return args.handler(cfg, args)
    except (ConfigError, RoutingError) as exc:
        print(f"configuratiefout: {exc}")
        return CONFIG_ERROR
    except KeyboardInterrupt:
        return INTERRUPTED


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agency_os", description="Spil, de dispatcher van Raderwerk.")
    subs = parser.add_subparsers(dest="command", required=True)

    run = subs.add_parser("run", help="één cyclus of de lus draaien")
    mode = run.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="precies één cyclus")
    mode.add_argument("--loop", action="store_true", help="blijven draaien")
    run.add_argument("--interval", type=int, default=None, help="seconden tussen cycli")
    run.add_argument("--issue", default=None, help="beperk de cyclus tot dit issue")
    run.add_argument("--max-claims", type=int, default=None, help="claims per cyclus")
    run.set_defaults(handler=_cmd_run)

    status = subs.add_parser("status", help="gezondheid en herkomst van de configuratie")
    status.add_argument("--json", action="store_true", dest="as_json")
    status.set_defaults(handler=_cmd_status)

    dry = subs.add_parser("dry-run", help="volledige cyclus zonder te schrijven")
    dry.add_argument("--issue", default=None)
    dry.add_argument("--cycles", type=int, default=1)
    dry.set_defaults(handler=_cmd_dry_run)

    beat = subs.add_parser("heartbeat", help="hartslag schrijven of de wachthond draaien")
    beat.add_argument("--watchdog", action="store_true")
    beat.set_defaults(handler=_cmd_heartbeat)

    book = subs.add_parser("ledger", help="het Kostenboek of het ruwe logboek")
    book.add_argument("--since", default=None)
    book.add_argument("--until", default=None)
    book.add_argument("--format", default="markdown", choices=("markdown", "json"), dest="fmt")
    book.add_argument("--logbook", action="store_true")
    book.set_defaults(handler=_cmd_ledger)
    return parser


def _overrides(args: argparse.Namespace) -> dict[str, str]:
    """Commandoregelvlaggen die de config overrulen."""
    values: dict[str, str] = {}
    if getattr(args, "interval", None):
        values["SPIL_INTERVAL_S"] = str(args.interval)
    if getattr(args, "max_claims", None):
        values["SPIL_MAX_CLAIMS_PER_CYCLE"] = str(args.max_claims)
    return values


def _cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    ctx = scheduler.build_context(cfg, only_issue=args.issue)
    if args.once:
        report = scheduler.run_cycle(ctx, 1)
        _print_report(report)
        return UNHEALTHY if report.errors or report.halted else OK

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    scheduler.run_loop(ctx, cfg.interval_s, stop=stop)
    return OK


def _cmd_dry_run(cfg: Config, args: argparse.Namespace) -> int:
    dry = cfg.with_overrides(dry_run=True)
    ctx = scheduler.build_context(dry, only_issue=args.issue, extra_sinks=(_PrintSink(),))
    code = OK
    for cycle in range(1, max(1, args.cycles) + 1):
        report = scheduler.run_cycle(ctx, cycle)
        _print_report(report)
        if report.errors:
            code = UNHEALTHY
    return code


def _cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    ctx = scheduler.build_context(cfg)
    state: dict[str, Any] = {"config": cfg.redacted(), "gezond": True, "problemen": []}

    for name, binary in (
        ("claude", cfg.executors.claude_bin),
        ("codex", cfg.executors.codex_bin),
        ("gh", cfg.executors.gh_bin),
        ("git", cfg.executors.git_bin),
    ):
        found = shutil.which(binary)
        state.setdefault("uitvoerders", {})[name] = found or "niet gevonden"
        if not found:
            state["problemen"].append(f"{name} ({binary}) staat niet in PATH")

    try:
        panel = ctx.client.issue(cfg.panel_identifier)
        state["paneel"] = {"issue": panel.identifier, "status": panel.state_name, "labels": list(panel.labels)}
        state["noodstop"] = "schakelaar/pauze-alles" in panel.labels
        if state["noodstop"]:
            state["problemen"].append("schakelaar/pauze-alles staat aan: er start niets")
        state["issueteller"] = ctx.client.organization_issue_count()
        if state["issueteller"] >= cfg.issue_budget[0]:
            state["problemen"].append(f"issueteller {state['issueteller']} boven de waarschuwingsgrens")
    except Exception as exc:
        state["problemen"].append(f"Linear niet bereikbaar: {exc}")

    last = ctx.store.last_heartbeat_at()
    state["laatste_hartslag"] = last.isoformat() if last else None
    if last is not None:
        age = (datetime.now(timezone.utc) - last).total_seconds()
        if age > cfg.watchdog_max_age_s:
            state["problemen"].append(f"laatste hartslag is {int(age // 60)} minuten oud")
    state["open_claims"] = [dict(row) for row in ctx.store.open_claims()]
    state["gezond"] = not state["problemen"]

    if args.as_json:
        print(json.dumps(state, indent=2, ensure_ascii=False, default=str))
    else:
        _print_status(state)
    return OK if state["gezond"] else UNHEALTHY


def _cmd_heartbeat(cfg: Config, args: argparse.Namespace) -> int:
    ctx = scheduler.build_context(cfg)
    if args.watchdog:
        code = heartbeat_mod.watchdog(ctx.client, ctx.store, cfg)
        print({0: "dispatcher leeft", 1: "motor-dood gezet", 2: "niet vast te stellen"}[code])
        return OK if code == heartbeat_mod.ALIVE else UNHEALTHY
    panel = ctx.client.issue(cfg.panel_identifier)
    heartbeat_mod.beat(ctx.client, ctx.store, cfg, panel, run_id=secrets.token_hex(3))
    return OK


def _cmd_ledger(cfg: Config, args: argparse.Namespace) -> int:
    from agency_os.linear import ledger as ledger_mod

    since = _as_day(args.since) or date.today()
    until = _as_day(args.until) or date.today()
    if args.logbook:
        print(Logbook(cfg.logbook_dir).export(since, until))
        return OK
    ctx = scheduler.build_context(cfg)
    if args.fmt == "json":
        day, rows = since, []
        while day <= until:
            rollup = ledger_mod.rollup(ctx.store, day)
            rows.append({"dag": day.isoformat(), "runs": rollup.runs, "usd": rollup.usd, "eur": rollup.eur})
            day = date.fromordinal(day.toordinal() + 1)
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        print(ledger_mod.render_markdown(ctx.store, since=since, until=until, prices=cfg.prices, fx=cfg.fx))
    return OK


def _as_day(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    if value == "today":
        return date.today()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"datum moet JJJJ-MM-DD zijn, kreeg {value!r}") from exc


class _PrintSink:
    """Toont in een droogloop precies de mutatie die geschreven zou zijn."""

    def record(self, m: Any) -> None:
        print(f"[droogloop] {m.mutation} op {m.entity_id}: {dict(m.variables_summary)}")


def _print_report(report: scheduler.CycleReport) -> None:
    print(
        f"cyclus {report.cycle}: {report.polled} gelezen, {report.claimed} geclaimd, "
        f"{report.finished} afgerond, {report.gates_seen} poorten gezien "
        f"({report.gates_applied} toegepast){', GESTOPT' if report.halted else ''}"
    )
    for problem in report.errors:
        print(f"  fout: {problem}")


def _print_status(state: dict) -> None:
    print(f"sleutelbron: {state['config']['linear_api_key_source']}")
    print(f"endpoint:    {state['config']['linear_endpoint']}")
    print(f"paneel:      {state.get('paneel', {}).get('issue', '?')}")
    print(f"hartslag:    {state['laatste_hartslag'] or 'nooit'}")
    print(f"issues:      {state.get('issueteller', '?')} (noodstop bij {state['config']['issue_budget'][2]})")
    print(f"open claims: {len(state['open_claims'])}")
    print(f"uitvoerders: {state.get('uitvoerders', {})}")
    for problem in state["problemen"]:
        print(f"probleem:    {problem}")
    print("gezond" if state["gezond"] else "ongezond")
