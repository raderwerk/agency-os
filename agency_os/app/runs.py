"""Eén run: de opdracht, de uitvoering en de drie schrijfacties terug.

Afgesplitst van `scheduler.py` omdat dat bestand anders ruim boven de 400 regels
uitkomt (architectuur sectie 2, groottediscipline). De planner beslist wát er
draait; dit bestand doet één run van begin tot eind, inclusief het uitvoercontract
van spec 8.3: één comment, één issueUpdate, eventueel bijlagen, altijd een
ledgerregel.

`ctx` is de `Context` van de planner. Die staat hier bewust als `Any` in de
handtekeningen: het alternatief is een kringimport tussen twee helften van
hetzelfde proces.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Optional

from agency_os.app import prompts
from agency_os.app.routing import Route
from agency_os.executors import base as executors
from agency_os.executors import cost, worktree
from agency_os.linear import claim as claims
from agency_os.linear import comments, gates, ledger, machine
from agency_os.linear.client import LinearError
from agency_os.linear.models import RunRecord

FINAL_LABEL = {
    "klaar": "run/klaar",
    "vraag": "run/wachtrij",
    "mislukt": "run/mislukt",
    "afgebroken": "run/wachtrij",
}
MAX_FAILURES_PER_STATE = 2


@dataclass
class Job:
    """Eén geclaimd issue met zijn route, onderweg door deze cyclus."""

    run_id: str
    issue: Any
    route: Route
    claim: Any
    state: str


def execute(ctx: Any, jobs: list[Job], errors: list[str], *, guard: Callable) -> int:
    """Draait de synchrone runs parallel en start de native sessies."""
    finished = 0
    sync: list[Job] = []
    for job in jobs:
        executor = ctx.executors.get(job.route.executor_name)
        if executor is None:
            errors.append(f"onbekende uitvoerder {job.route.executor_name} voor {job.issue.identifier}")
        elif hasattr(executor, "trigger"):
            guard(errors, f"start {job.issue.identifier}", lambda job=job, ex=executor: start_native(ctx, job, ex))
        else:
            sync.append(job)

    if sync:
        with ThreadPoolExecutor(max_workers=max(1, ctx.cfg.max_concurrent_runs)) as pool:
            for job, result in zip(sync, pool.map(lambda j: run_one(ctx, j), sync)):
                guard(errors, f"terugschrijven {job.issue.identifier}", lambda job=job, r=result: finish(ctx, job, r))
                finished += 1
    return finished


def run_one(ctx: Any, job: Job) -> Any:
    """Draait één synchrone uitvoerder; een uitzondering wordt 'mislukt'."""
    started = ctx.now()
    try:
        return ctx.executors[job.route.executor_name].run(request_for(ctx, job))
    except Exception as exc:
        return executors.ExecutionResult(
            run_id=job.run_id,
            uitkomst="mislukt",
            summary_md=f"De uitvoerder {job.route.executor_name} stopte met een fout.",
            dod="-",
            question=None,
            error=str(exc),
            pr_url=None,
            branch=None,
            artifacts=(),
            usage=executors.Usage(),
            started_at=started,
            ended_at=ctx.now(),
            session_id=None,
            raw_log_path=None,
        )


def start_native(ctx: Any, job: Job, executor: Any) -> None:
    """Native lane: één mention-comment, daarna wachten in latere cycli."""
    receipt = executor.trigger(ctx.client, request_for(ctx, job))
    ctx.receipts[job.issue.id] = (receipt, job)
    ctx.logbook.write(
        "run",
        run_id=job.run_id,
        issue=job.issue.identifier,
        payload={"native": job.route.executor_name, "sessie": receipt.session_id},
    )


def collect_native(ctx: Any, watching: dict[str, Any], errors: list[str], *, guard: Callable) -> int:
    """Kijkt bij de lopende native sessies of er al een uitkomst is."""
    finished = 0
    for issue_id, (receipt, job) in list(ctx.receipts.items()):
        issue = watching.get(issue_id, job.issue)
        executor = ctx.executors.get(job.route.executor_name)
        if executor is None:
            continue
        try:
            receipt, outcome = executor.poll(ctx.client, receipt, issue)
        except LinearError as exc:
            errors.append(f"sessie {issue.identifier}: {exc}")
            continue
        ctx.receipts[issue_id] = (receipt, job)
        if outcome is None:
            continue
        del ctx.receipts[issue_id]
        guard(errors, f"terugschrijven {issue.identifier}", lambda job=job, r=outcome: finish(ctx, job, r))
        finished += 1
    return finished


def request_for(ctx: Any, job: Job) -> Any:
    """De opdracht voor B: prompt, repo, branch en de grenzen eromheen."""
    role, issue = job.route.role, job.issue
    contract = getattr(issue, "contract", None)
    return executors.ExecutionRequest(
        run_id=job.run_id,
        issue=issue,
        role_key=role.key,
        role_title=role.title,
        model_key=job.route.model_key,
        model_display=job.route.model.display,
        model_ledger=job.route.model.ledger,
        prompt=prompts.build_prompt(ctx.cfg, role, issue, run_id=job.run_id),
        repo=issue.repo,
        base_branch=getattr(contract, "basisbranch", None) or "main",
        branch=worktree.branch_name(issue.identifier, issue.title) if role.needs_worktree else "",
        needs_worktree=role.needs_worktree,
        needs_pr=role.needs_pr,
        pr_title=f"{issue.identifier}: {issue.title}",
        pr_body=f"Linear: {issue.url}\n\nRun {job.run_id}, rol {role.title}.",
        timeout_s=ctx.cfg.executors.run_timeout_s,
        dry_run=ctx.cfg.dry_run,
    )


def finish(ctx: Any, job: Job, result: Any) -> None:
    """Het uitvoercontract: één comment, één issueUpdate, eventueel bijlagen."""
    issue, role = job.issue, job.route.role
    target, extra_labels, assignee = outcome_of(ctx, job, result)
    run = run_record(ctx, job, result, target)

    ctx.client.create_comment(
        issue.id,
        comments.run_comment(
            role_title=role.title,
            model_display=job.route.model.display,
            run=run,
            body_md=result.summary_md,
            evidence=result.artifacts,
            dod=result.dod,
            next_state=target or job.state,
        ),
        run_id=job.run_id,
    )

    if target and machine.is_gate(target):
        gates.enter_gate(
            ctx.client,
            issue,
            run_id=job.run_id,
            gate_state=target,
            approver_id=sorted(ctx.cfg.approver_ids)[0],
            card_markdown=gate_card(ctx, job, result, run),
            artefact_url=result.pr_url,
        )
    elif target or extra_labels or assignee:
        ctx.client.update_issue(
            issue.id,
            run_id=job.run_id,
            state=target if target and target != job.state else None,
            added_labels=extra_labels,
            assignee_id=assignee,
        )

    for artifact in result.artifacts:
        ctx.client.attach_link(issue.id, artifact.url, artifact.label or artifact.type, run_id=job.run_id)

    ledger.record_run(ctx.store, run)
    claims.release_claim(ctx.client, ctx.store, job.claim, final_label=FINAL_LABEL[result.uitkomst])
    ctx.logbook.write(
        "run",
        run_id=job.run_id,
        issue=issue.identifier,
        payload={"uitkomst": result.uitkomst, "volgende_status": target, "kosten_eur": run.kosten_eur},
    )


def outcome_of(ctx: Any, job: Job, result: Any) -> tuple[Optional[str], list[str], Optional[str]]:
    """Vertaalt de uitkomst naar (volgende status, extra labels, toegewezene)."""
    team = job.issue.team_key
    if result.uitkomst == "klaar":
        return machine.next_state(team, job.state, "klaar"), [], None
    if result.uitkomst == "afgebroken":
        return None, [], None
    if result.uitkomst == "mislukt":
        key = (job.issue.id, job.state)
        ctx.failures[key] = ctx.failures.get(key, 0) + 1
        if ctx.failures[key] < MAX_FAILURES_PER_STATE:
            return None, [], None
    # vraag, en de tweede mislukking op dezelfde status: een mens is aan zet
    return machine.WAIT_STATE[team], ["schakelaar/mens-vereist"], sorted(ctx.cfg.approver_ids)[0]


def run_record(ctx: Any, job: Job, result: Any, target: Optional[str]) -> RunRecord:
    """Eén ledgerregel, ook bij mislukken: een boek met alleen successen is reclame."""
    usage = cost.normalise(result.usage, job.route.model.ledger, ctx.cfg.prices)
    return RunRecord(
        run_id=job.run_id,
        issue_id=job.issue.id,
        issue_identifier=job.issue.identifier,
        team_key=job.issue.team_key,
        rol=job.route.role.key,
        model=job.route.model.ledger,
        executor=job.route.executor_name,
        klant=job.issue.klant,
        dienst=job.issue.dienst,
        gestart=result.started_at,
        geeindigd=result.ended_at,
        duur_s=usage.duration_s,
        beurten=usage.turns,
        tokens_in=usage.tokens_in,
        tokens_uit=usage.tokens_out,
        cache_lees=usage.cache_read,
        kosten_usd=usage.cost_usd,
        kosten_eur=cost.to_eur(usage.cost_usd, ctx.cfg.fx),
        dod=result.dod,
        uitkomst=result.uitkomst,
        volgende_status=target,
        pr_url=result.pr_url,
        artefacten=tuple(result.artifacts),
        metered=usage.metered,
    )


def gate_card(ctx: Any, job: Job, result: Any, run: RunRecord) -> str:
    """De poortkaart die de mens leest voordat hij mergt of publiceert."""
    criteria = prompts.acceptance_criteria(job.issue.description or "")
    return comments.gate_card(
        gate_no=machine.next_state(job.issue.team_key, job.state, "klaar") or "Poort",
        issue=job.issue,
        what=result.summary_md,
        evidence=result.artifacts,
        criteria="\n".join(f"- {item}" for item in criteria) or "geen acceptatiecriteria in het issue",
        reviewers=f"{job.route.role.title} op {job.route.model.display}",
        disagreement="geen tweede oordeel in deze run",
        risk=job.issue.risico,
        cost_so_far=f"€ {run.kosten_eur:.2f}",
        high_risk=job.issue.high_risk,
        run_id=job.run_id,
        duration_s=run.duur_s,
        cost_eur=run.kosten_eur,
    )
