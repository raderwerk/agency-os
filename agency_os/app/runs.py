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
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

from agency_os.app import evidence, prompts
from agency_os.app.routing import Route
from agency_os.executors import base as executors
from agency_os.executors import cost, worktree
from agency_os.linear import claim as claims
from agency_os.linear import comments, gates, ledger, machine
from agency_os.linear.client import LinearError, WriteRefused
from agency_os.linear.models import Artifact, Claim, RunRecord
from agency_os.linear.store import parse_iso

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
            # Een uitzondering uit de laan zelf: er is geen model aan te pas
            # gekomen, dus dit is geen poging van de rol.
            infra_failure=True,
        )


def start_native(ctx: Any, job: Job, executor: Any) -> None:
    """Native lane: één mention-comment, daarna wachten in latere cycli.

    De bon gaat óók naar sqlite. Een mention start een echte, betaalde
    cloudsessie; als die alleen in het geheugen van dit proces bestaat, is de
    sessie na `run --once` niet meer terug te vinden, blijft `run/bezet` staan
    en komt er nooit een ledgerregel.
    """
    receipt = executor.trigger(ctx.client, request_for(ctx, job))
    ctx.receipts[job.issue.id] = (receipt, job)
    ctx.store.upsert_session(
        issue_id=job.issue.id,
        run_id=job.run_id,
        executor=job.route.executor_name,
        session_id=receipt.session_id,
        trigger_comment_id=receipt.trigger_comment_id,
        triggered_at=receipt.triggered_at,
        last_status="aangestoten",
        strikes=receipt.strikes,
        role=job.route.role.key,
        model_key=job.route.model_key,
        state=job.state,
    )
    # Een regel in het Kostenboek op het moment van aanstoten, niet pas bij de
    # uitkomst: de sessie is vanaf nu betaald werk. `finish` schrijft dezelfde
    # `run_id` later over met de echte cijfers.
    ledger.record_run(ctx.store, run_record(ctx, job, _pending(receipt), None))
    ctx.logbook.write(
        "run",
        run_id=job.run_id,
        issue=job.issue.identifier,
        payload={"native": job.route.executor_name, "sessie": receipt.session_id},
    )


def _pending(receipt: Any) -> Any:
    """De vorm van een run die wel begonnen maar nog niet afgelopen is."""
    return executors.ExecutionResult(
        run_id=receipt.run_id,
        uitkomst="bezig",
        summary_md="",
        dod="-",
        question=None,
        error=None,
        pr_url=None,
        branch=None,
        artifacts=(),
        usage=executors.Usage(source="native-unmetered", metered=False),
        started_at=receipt.triggered_at,
        ended_at=None,
        session_id=receipt.session_id,
        raw_log_path=None,
    )


def collect_native(ctx: Any, watching: dict[str, Any], errors: list[str], *, guard: Callable) -> int:
    """Kijkt bij de lopende native sessies of er al een uitkomst is."""
    rehydrate(ctx, watching, errors)
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
        ctx.store.upsert_session(
            issue_id=issue_id, run_id=receipt.run_id, executor=job.route.executor_name,
            session_id=receipt.session_id, trigger_comment_id=receipt.trigger_comment_id,
            triggered_at=receipt.triggered_at,
            last_status="klaar" if outcome else "loopt", strikes=receipt.strikes,
            closed_at=ctx.now() if outcome else None,
        )
        if outcome is None:
            continue
        del ctx.receipts[issue_id]
        ctx.handled.add(issue_id)
        guard(errors, f"terugschrijven {issue.identifier}", lambda job=job, r=outcome: finish(ctx, job, r))
        finished += 1
    return finished


def rehydrate(ctx: Any, watching: dict[str, Any], errors: list[str]) -> int:
    """Haalt aangestoten native sessies terug uit sqlite na een herstart.

    `ctx.receipts` leeft in het geheugen van één proces. Zonder deze stap is een
    Codex- of Cursorsessie na `run --once` verweesd: het issue houdt `run/bezet`,
    de claim blijft open en niemand kijkt ooit nog of de sessie klaar is.

    De route wordt niet opnieuw afgeleid maar teruggelezen: tijdens de run staat
    het issue in een status waar de routeringstabel geen regel voor heeft, dus
    `decide` zou hier weigeren op een run die allang loopt.
    """
    from agency_os.app.routing import Route

    recovered = 0
    for row in ctx.store.open_sessions():
        issue_id = row["issue_id"]
        if issue_id in ctx.receipts:
            continue
        issue = watching.get(issue_id)
        if issue is None:
            continue
        role = ctx.table.roles.get(row["role"] or "")
        if role is None:
            errors.append(f"sessie {issue.identifier}: rol {row['role']!r} bestaat niet meer")
            continue
        claim_row = ctx.store.open_claim(issue_id)
        claim = Claim(
            run_id=row["run_id"], issue_id=issue_id, issue_identifier=issue.identifier,
            claimed_at=(parse_iso(claim_row["claimed_at"]) if claim_row
                        else parse_iso(row["triggered_at"])) or ctx.now(),
            comment_id=row["trigger_comment_id"],
        )
        job = Job(run_id=row["run_id"], issue=issue,
                  route=Route(role=role, model_key=row["model_key"] or role.default_model,
                              executor_name=row["executor"],
                              reason="hervat uit sqlite na een herstart"),
                  claim=claim, state=row["state"] or issue.state_name)
        ctx.receipts[issue_id] = (
            executors.TriggerReceipt(
                run_id=row["run_id"], issue_id=issue_id, executor=row["executor"],
                trigger_comment_id=row["trigger_comment_id"], session_id=row["session_id"],
                triggered_at=parse_iso(row["triggered_at"]) or ctx.now(),
                strikes=int(row["strikes"] or 0),
            ),
            job,
        )
        recovered += 1
    return recovered


def _discussion(ctx: Any, issue: Any) -> tuple:
    """De comments op het issue voor in de prompt; een leesfout kost geen run."""
    try:
        return tuple(ctx.client.comments(issue.id))
    except LinearError as exc:
        ctx.logbook.write("run", run_id=None, issue=issue.identifier,
                          payload={"discussie_niet_gelezen": str(exc)})
        return ()


def request_for(ctx: Any, job: Job) -> Any:
    """De opdracht voor B: prompt, repo, branch en de grenzen eromheen."""
    role, issue = job.route.role, job.issue
    contract = getattr(issue, "contract", None)
    base_branch = getattr(contract, "basisbranch", None) or "main"
    branch = worktree.branch_name(issue.identifier, issue.title) if role.needs_worktree else ""
    discussion = _discussion(ctx, issue)
    return executors.ExecutionRequest(
        run_id=job.run_id,
        issue=issue,
        role_key=role.key,
        role_title=role.title,
        model_key=job.route.model_key,
        model_display=job.route.model.display,
        model_ledger=job.route.model.ledger,
        prompt=prompts.build_prompt(
            ctx.cfg, role, issue, run_id=job.run_id,
            branch=branch, base_branch=base_branch, discussion=discussion,
            extra_context=evidence.for_role(ctx.cfg.executors, role, issue,
                                            branch=branch, discussion=discussion),
        ),
        repo=issue.repo,
        base_branch=base_branch,
        branch=branch,
        needs_worktree=role.needs_worktree,
        needs_pr=role.needs_pr,
        pr_title=f"{issue.identifier}: {issue.title}",
        pr_body=f"Linear: {issue.url}\n\nRun {job.run_id}, rol {role.title}.",
        timeout_s=ctx.cfg.executors.run_timeout_s,
        dry_run=ctx.cfg.dry_run,
    )


def with_pull_request(result: Any) -> tuple:
    """De pull request die de Spil zelf opende, vooraan in het bewijs.

    `_publish` opent de PR ná de modelrun, dus het RUNRESULT-blok kan hem niet
    kennen: een model dat netjes geen url verzint levert een leeg `bewijs` aan.
    Het gevolg stond letterlijk in de eerste geslaagde live run van WV-210:
    "Bewijs: geen bruikbare link", op een issue waar de Spil op dat moment
    https://github.com/raderwerk/raderwerk-content/pull/2 had geopend. Dezelfde
    lijst voedt de poortkaart, dus zonder deze regel leest een mens bij de poort
    een kaart zonder het enige artefact dat er toe doet.
    """
    artifacts = tuple(result.artifacts)
    url = getattr(result, "pr_url", None)
    if not url or any(a.url == url for a in artifacts):
        return artifacts
    number = url.rstrip("/").rsplit("/", 1)[-1]
    label = f"PR #{number}" if number.isdigit() else "pull request"
    return (Artifact("pr", url, label), *artifacts)


def finish(ctx: Any, job: Job, result: Any) -> None:
    """Het uitvoercontract: één comment, één issueUpdate, eventueel bijlagen."""
    issue, role = job.issue, job.route.role
    kept, refused = executors.safe_artifacts(with_pull_request(result))
    result = replace(result, artifacts=kept)
    target, extra_labels, assignee = outcome_of(ctx, job, result)
    run = run_record(ctx, job, result, target)

    # Het Kostenboek gaat vóór het netwerk. De tokens zijn hier al verbrand;
    # elke Linear-schrijfactie hierna kan een `LinearError` gooien, en die wordt
    # door `_guard` opgevangen. Stond deze regel onderaan, dan zou zo'n fout de
    # kosten uit het boek laten verdwijnen -- in een project waarvan de stelling
    # een eerlijk boek is.
    ledger.record_run(ctx.store, run)
    _discount_if_stalled(ctx, job, result)
    if refused:
        ctx.logbook.write("run", run_id=job.run_id, issue=issue.identifier,
                          payload={"geweigerd_bewijs": [a.url for a in refused]})

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
            refused=refused,
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

    _attach(ctx, job, result.artifacts)

    claims.release_claim(ctx.client, ctx.store, job.claim, final_label=FINAL_LABEL[result.uitkomst])
    ctx.store.close_session(issue.id, job.run_id, ctx.now())
    ctx.logbook.write(
        "run",
        run_id=job.run_id,
        issue=issue.identifier,
        payload={"uitkomst": result.uitkomst, "volgende_status": target, "kosten_eur": run.kosten_eur},
    )


def _discount_if_stalled(ctx: Any, job: Job, result: Any) -> None:
    """Een laan die niet startte kost de rol geen dagbeurt.

    `_claim` boekt de poging vóór de run, want een proces dat halverwege omvalt
    moet zijn beurt kwijt zijn. Pas hier is te zien of er ook echt iets gedraaid
    heeft. De dag is die van de start, niet die van nu: een native sessie kan
    over middernacht heen lopen en zou anders de beurt van morgen wegstrepen.
    """
    if not getattr(result, "infra_failure", False):
        return
    day = (result.started_at or ctx.now()).date()
    left = ctx.store.discount_role_run(job.issue.id, job.route.role.key, day)
    ctx.logbook.write("run", run_id=job.run_id, issue=job.issue.identifier,
                      payload={"lusdetectie_niet_geteld": result.error or "de laan startte niet",
                               "echte_runs_vandaag": left})


def _attach(ctx: Any, job: Job, artifacts: Any) -> None:
    """Bijlagen koppelen, maar nooit ten koste van het uitvoercontract.

    Een bijlage is een dubbeling: dezelfde link staat al in de comment. Linear
    weigert `attachmentLinkURL` op een url die een integratie al bezit -- de
    GitHub-koppeling had PR #2 zelf al aan WV-210 gehangen, en de tweede poging
    kwam terug als "Unable to create issue attachment". Zonder deze afscherming
    nam die fout de rest van `finish` mee: de claim werd niet vrijgegeven en
    `run/bezet` bleef staan op een run die allang klaar was.
    """
    for artifact in artifacts:
        try:
            ctx.client.attach_link(job.issue.id, artifact.url,
                                   artifact.label or artifact.type, run_id=job.run_id)
        except (LinearError, WriteRefused) as exc:
            ctx.logbook.write("run", run_id=job.run_id, issue=job.issue.identifier,
                              payload={"bijlage_niet_gekoppeld": artifact.url, "fout": str(exc)})


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
        risk_flags=job.issue.risico_flags,
        risk=job.issue.risico,
        cost_so_far=f"€ {run.kosten_eur:.2f}",
        high_risk=job.issue.high_risk,
        run_id=job.run_id,
        duration_s=run.duur_s,
        cost_eur=run.kosten_eur,
    )
