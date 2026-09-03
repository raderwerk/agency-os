# Spil MVP — architecture

Status: design, approved for build. Date: 2026-09-03. Language: English (code, commits, PRs);
everything Spil writes into Linear stays Dutch.

Authority: `hq/design/linear-workspace-spec.md` (chapters 2, 3, 5, 7, 8, 9, 11) and
`hq/design/agent-roster.md` are the specification. This document is the *implementation* plan for
the smallest thing that can run unattended today. Where this document deviates from the spec, the
deviation is named in [§18](#18-deviations-from-the-spec-and-honest-gaps) with a reason. Nothing is
silently narrowed.

---

## 1. What the MVP is

One Python process, standard library only, Python 3.12.

```
python -m agency_os run --loop --interval 60
```

Every 60 seconds it does one batched Linear read, decides what may move, claims at most four
issues, runs at most two of them concurrently, and writes back exactly one signed comment plus one
`issueUpdate` per run. It stops at every status whose name starts with `Poort`. It logs every
mutation. It costs money and records what it cost.

That is the whole product. Everything else in this document exists to make that sentence true
without lying anywhere.

### In scope

| Board | States the MVP drives |
|---|---|
| WV | `Ingepland` → `In uitvoering` → `Agentreview` → `QA op preview` → `Poort · Merge of publicatie` → *(human)* → `Na-merge controle` → `Klaar` |
| KR | `Lead` → `Gekwalificeerd`, `Discovery` → `Voorstel`, `Voorstel` → `Poort 1 · Voorstel akkoord` |

Plus, on both boards: `Wacht op input` on an agent question, gate evaluation on every gate state,
the kill switch, the heartbeat, the watchdog and the ledger.

### Explicitly out of scope for the MVP

`Binnen` triage on either board, `Kickoff` (project and issue creation by PM), `Klantacceptatie`
(Klantstem), `Retainer`, Poort 2 and Poort 3 *transitions* (they are still **read** and their gate
cards are still honoured — Spil just has no role that produces the artefacts behind them yet), and
the pre-approval rule of spec §7.7. An issue in an out-of-scope state is polled, logged as
`buiten-mvp` and never claimed. It is never half-processed.

### Non-goals

No webhooks (our app is deliberately not `app:assignable`, spec §8.1). No database server. No web
UI — the Linear board *is* the UI. No cost cap: cost is logged, loops are detected (spec §8.6).

---

## 2. Module split, ownership, and file map

Three engineers, three disjoint file sets, one dependency direction: **C → B → A → stdlib**. No
module ever imports "upward". Every cross-module type is defined in the module *lower* in that
chain, so no engineer has to wait for another to define a name.

```
agency_os/
  __init__.py                 (exists — A may bump __version__, nobody else touches it)
  __main__.py                 C
  gate.py                     A  (exists; A extends it)
  linear/                     A
    __init__.py  client.py  models.py  queries.py  poll.py  claim.py
    machine.py  gates.py  killswitch.py  comments.py  store.py  ledger.py
  executors/                  B
    __init__.py  base.py  worktree.py  gh.py  claude_runner.py
    native.py  codex_cli.py  cost.py
  app/                        C
    __init__.py  cli.py  config.py  scheduler.py  routing.py
    prompts.py  logbook.py  heartbeat.py
  roles/                      C  (data, not code)
    routing.json  _skelet.md  redacteur.md  ontwikkelaar.md  ontwerper.md
    campagneplanner.md  reviewer.md  qa.md  account.md  strateeg.md
tests/
  __init__.py                 (exists)
  test_gate.py                A  (exists)
  test_linear_*.py            A
  test_executors_*.py         B
  test_app_*.py               C
  fakes.py                    C  (shared fakes; C writes it FIRST, day 1 — see §16)
docs/architecture.md          this file
README.md  AGENTS.md          C
```

**File-disjointness rule.** A file has exactly one owner. `tests/fakes.py` is owned by C but is a
day-1 deliverable precisely because A and B depend on it; its contract is frozen in
[§3.8](#38-test-fakes-owned-by-c-frozen-on-day-1) so C cannot change it under A and B without a
diff review from both.

**Size discipline.** No file above 400 lines. If `client.py` or `scheduler.py` heads past that,
split it (`queries.py` already exists to absorb GraphQL strings; `scheduler.py` may spawn
`scheduler_wv.py` / `scheduler_kr.py`, still owned by C).

### A — `agency_os/linear/` (+ `gate.py`)

Owns everything that talks to Linear and everything that remembers.

| File | Responsibility |
|---|---|
| `client.py` | GraphQL transport: auth header (`Authorization: <key>`, no `Bearer`), retries, rate-limit budget, `dry_run`, mutation sinks, **write guards** |
| `models.py` | Every shared dataclass (§3.2). Pure data, no I/O |
| `queries.py` | Every GraphQL document as a module constant; field lists live here and nowhere else |
| `poll.py` | The one batched read per cycle; classification into ready / gate / watching |
| `claim.py` | Claim protocol: label lock + settle + read-back + sqlite lock + idempotency probes |
| `machine.py` | State-transition tables as data + pure lookup functions |
| `gates.py` | Gate detection, the five validity conditions, the six entry actions, approve/reject handling |
| `killswitch.py` | Reading `schakelaar/*`, the halt procedure, the issue-count budget guard |
| `comments.py` | Rendering and posting every comment format (signature, claim, run, gate card, QA report, halt, fallback) |
| `store.py` | SQLite schema, migrations, all queries against it |
| `ledger.py` | `RunRecord` persistence, the yaml tail block (render + parse), roll-ups, markdown export |

### B — `agency_os/executors/`

Owns everything that runs something outside this process: a CLI, git, gh, a native agent.

| File | Responsibility |
|---|---|
| `base.py` | `ExecutionRequest`, `ExecutionResult`, `Usage`, `Artifact`, `ExecutorConfig`, the two executor protocols, `assert_safe_worktree` |
| `worktree.py` | `git worktree` create/remove, branch naming, slugging, existing-branch probe |
| `gh.py` | `gh` CLI wrapper: open PR, find PR for branch, read PR state / merged-by / checks |
| `claude_runner.py` | `claude -p --output-format json`, timeout, kill-group, RUNRESULT parsing |
| `native.py` | Codex/Cursor mention trigger, `delegateId`, `agentSessions` watcher, PR-link extraction, two-strike fallback |
| `codex_cli.py` | `codex exec` second reviewer |
| `cost.py` | Usage normalisation, price table, USD→EUR, `budget-let-op` threshold |

B never imports `agency_os.app`. B may import `agency_os.linear.models` (for `IssueView`) and
nothing else from A — in particular **B never writes to Linear**. Every Linear write in the whole
system goes through A. That is what makes "every mutation logged" enforceable in one place.

### C — `agency_os/app/`, `agency_os/roles/`, tests, README

| File | Responsibility |
|---|---|
| `cli.py` | Argument parsing, the six subcommands, exit codes |
| `config.py` | Env + `~/.config/raderwerk/spil.env`, defaults, validation, `ExecutorConfig` construction |
| `scheduler.py` | The cycle: poll → switches → gates → claim → route → execute → write back |
| `routing.py` | Loads `roles/routing.json`, resolves `(team, state, labels) → RoleSpec`, model override, loop detection |
| `prompts.py` | Assembles the role prompt: skeleton + role block + issue + repo AGENTS.md + output contract |
| `logbook.py` | Append-only JSONL sink (also registered as a `MutationSink`), export |
| `heartbeat.py` | Heartbeat comment + panel counters; the watchdog check |

---

## 3. Shared contracts

These are frozen. Three engineers build against exactly this and do not negotiate.

### 3.1 Vocabulary and identifier rules

* **Issue identifier** — `"WV-207"`. The Linear API accepts it wherever a `String!` id is expected
  (`issue(id: "WV-207")` verified 2026-09-03). Always resolve by identifier or by name at startup.
  **Do not trust `hq/linear/idmap.json` UUIDs**: verified 2026-09-03 that at least one entry
  (`Publiek bouwlogboek` → `...4897170ada02`) does not resolve, while the live id is
  `...4897170ad34d`. `idmap.json` is a hint for humans, never an input to the dispatcher.
* **Canonical label name** — Linear returns *leaf* names. `Issue.labels.nodes[].name` for
  `soort/contentstuk` is `"contentstuk"`, with the group in `parent.name`. The client MUST build
  the canonical name as `f"{parent.name}/{name}"` when `parent` is present, else `name`. Verified
  on WV-207. `repo/raderwerk/raderwerk-content` is stored as leaf `"raderwerk/raderwerk-content"`
  under parent `"repo"`, so its canonical name has two slashes. Every comparison in the codebase
  uses the canonical name.
* **run_id** — 6 lowercase hex characters, generated once per run by C
  (`secrets.token_hex(3)`), carried by every write of that run.
* **Timestamps** — timezone-aware UTC `datetime` everywhere in Python; ISO-8601 with `Z` in JSON.
  Display in comments uses Europe/Amsterdam local time, formatted `2026-09-03 11:14`.
* **Money** — floats, rounded to cents only at render time. USD is primary; EUR is derived with
  the configured rate and never hardcoded.

### 3.2 `agency_os/linear/models.py` — owned by A

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional, Sequence

# ---------- issue side ----------

@dataclass(frozen=True)
class Contract:
    """The first yaml block under '## Opdrachtcontract'. Unknown keys are kept, not dropped."""
    version: str                       # "v1"
    klant: Optional[str]
    repo: Optional[str]                # "raderwerk/raderwerk-content"
    basisbranch: str                   # default "main"
    omgeving: str                      # "preview" | "dev-store" | "geen"
    publiek: bool
    bronnen: tuple[str, ...]
    verboden: tuple[str, ...]
    unknown_keys: tuple[str, ...]
    raw: str                           # the literal yaml text, for the prompt

    @staticmethod
    def parse(description: str) -> Optional["Contract"]: ...

@dataclass(frozen=True)
class IssueView:
    id: str                            # uuid
    identifier: str                    # "WV-207"
    title: str
    description: str
    url: str
    team_key: str                      # "WV" | "KR"
    state_id: str
    state_name: str                    # "Ingepland"
    state_type: str                    # backlog|unstarted|started|completed|canceled|triage
    estimate: Optional[int]            # 1..5
    priority: int                      # 0..4
    labels: tuple[str, ...]            # canonical names, sorted
    label_ids: Mapping[str, str]       # canonical name -> label uuid
    project_id: Optional[str]
    project_name: Optional[str]
    assignee_id: Optional[str]
    delegate_id: Optional[str]
    updated_at: datetime
    contract: Optional[Contract]

    # derived, all pure
    def label_in_group(self, group: str) -> Optional[str]: ...   # "dienst" -> "content"
    @property
    def dienst(self) -> Optional[str]: ...        # "content"
    @property
    def soort(self) -> Optional[str]: ...         # "contentstuk"
    @property
    def klant(self) -> Optional[str]: ...         # "raderwerk"
    @property
    def risico(self) -> str: ...                  # "laag" when the group label is absent
    @property
    def agent_hint(self) -> Optional[str]: ...    # "sonnet" | "codex" | ... | None
    @property
    def run_state(self) -> Optional[str]: ...     # "bezet" | "wachtrij" | ... | None
    @property
    def repo(self) -> Optional[str]: ...          # repo/* label, else contract.repo, else None
    @property
    def high_risk(self) -> bool: ...              # risico == "hoog"
    @property
    def is_gate_state(self) -> bool: ...          # state_name.startswith("Poort")
    @property
    def flags(self) -> frozenset[str]: ...        # ungrouped flags present on the issue

@dataclass(frozen=True)
class CommentView:
    id: str
    body: str
    created_at: datetime
    author_id: str
    author_name: str
    author_is_app: bool                # User.app

@dataclass(frozen=True)
class ActivityView:
    id: str
    type: str                          # thought|action|response|error|elicitation|prompt
    body: str
    created_at: datetime

@dataclass(frozen=True)
class AgentSessionView:
    id: str
    status: str                        # pending|active|awaitingInput|complete|error|stale
    summary: Optional[str]
    app_user_id: str
    app_user_name: str                 # "Codex" | "Cursor"
    created_at: datetime
    updated_at: datetime
    activities: tuple[ActivityView, ...]
    pull_request_url: Optional[str]

# ---------- run side ----------

@dataclass(frozen=True)
class Artifact:
    type: str                          # "pr" | "preview" | "document" | "screenshot" | "test"
    url: str
    label: str = ""

@dataclass(frozen=True)
class RunRecord:
    run_id: str
    issue_id: str
    issue_identifier: str
    team_key: str
    rol: str                           # routing key: "redacteur"
    model: str                         # ledger name: "claude-sonnet-5"
    executor: str                      # "claude" | "native-codex" | "native-cursor" | "codex-cli"
    klant: Optional[str]
    dienst: Optional[str]
    gestart: datetime
    geeindigd: Optional[datetime]
    duur_s: float
    beurten: int
    tokens_in: int
    tokens_uit: int
    cache_lees: int
    kosten_usd: float
    kosten_eur: float
    dod: str                           # "6/6" or "-"
    uitkomst: str                      # klaar | vraag | mislukt | afgebroken
    volgende_status: Optional[str]
    pr_url: Optional[str]
    artefacten: tuple[Artifact, ...]
    metered: bool                      # False for native lanes (spec ch. 11 honesty rule)

# ---------- machine side ----------

@dataclass(frozen=True)
class SwitchState:
    global_pause: bool
    paused_issue_ids: frozenset[str]
    engine_dead: bool
    issue_count: int                   # organization.createdIssueCount
    budget_level: str                  # "ok" | "warn" | "restrict" | "stop"
    reason: Optional[str]

@dataclass(frozen=True)
class Claim:
    run_id: str
    issue_id: str
    issue_identifier: str
    claimed_at: datetime
    comment_id: Optional[str]          # None in dry-run

@dataclass(frozen=True)
class GateObservation:
    issue_id: str
    gate_state: str
    card_comment_id: Optional[str]
    card_created_at: Optional[datetime]
    outcome: Optional[str]             # "akkoord" | "afgekeurd" | None (no decision yet)
    token: Optional[str]
    source: Optional[str]              # "comment" | "label"
    source_id: Optional[str]
    actor_id: Optional[str]
    actor_name: Optional[str]
    actor_is_app: Optional[bool]
    valid: bool
    refusal: Optional[str]             # which of the five conditions failed, in Dutch

@dataclass(frozen=True)
class MutationRecord:
    at: datetime
    run_id: Optional[str]
    mutation: str                      # "issueUpdate"
    entity_id: str
    variables_digest: str              # sha256 hex of the canonical json, never raw values
    variables_summary: Mapping[str, object]   # allowlisted keys only (stateId, addedLabelIds, ...)
    result_id: Optional[str]
    ok: bool
    error: Optional[str]
    dry_run: bool

@dataclass(frozen=True)
class PollResult:
    at: datetime
    panel: Optional[IssueView]
    switches: SwitchState
    ready: tuple[IssueView, ...]       # claimable, already sorted per spec 8.1 step 5
    gates: tuple[IssueView, ...]       # sitting in a gate state, to be evaluated
    watching: tuple[IssueView, ...]    # run/bezet and owned by us (native sessions, resumes)
    skipped: tuple[tuple[str, str], ...]   # (identifier, reason) — logged, never acted on
```

### 3.3 `agency_os/linear/client.py` — owned by A

```python
class MutationSink(Protocol):
    def record(self, m: MutationRecord) -> None: ...

class LinearError(RuntimeError):
    errors: list[dict]
    def matches(self, *needles: str) -> bool: ...

class WriteRefused(RuntimeError):
    """A write guard refused. Never caught and retried — it means the code is wrong."""

class LinearClient:
    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = "https://api.linear.app/graphql",
        dispatcher_user_id: str,
        sinks: Sequence[MutationSink] = (),
        dry_run: bool = False,
        timeout_s: int = 90,
    ) -> None: ...

    # reads
    def query(self, document: str, variables: dict | None = None) -> dict: ...
    def paginate(self, document: str, path: str, variables: dict | None = None,
                 page_size: int = 50) -> list[dict]: ...
    def issue(self, identifier_or_id: str) -> IssueView: ...
    def comments(self, issue_id: str, *, limit: int = 50) -> list[CommentView]: ...
    def agent_sessions(self, issue_id: str) -> list[AgentSessionView]: ...
    def label_ids(self) -> Mapping[str, str]: ...      # canonical name -> uuid, cached
    def state_ids(self, team_key: str) -> Mapping[str, str]: ...   # state name -> uuid, cached
    def organization_issue_count(self) -> int: ...

    # writes — every one of these logs a MutationRecord to every sink, always
    def create_comment(self, issue_id: str, body: str, *, run_id: str) -> Optional[str]: ...
    def update_issue(
        self, issue_id: str, *, run_id: str,
        state: str | None = None,               # state NAME, resolved internally
        added_labels: Sequence[str] = (),       # canonical NAMES
        removed_labels: Sequence[str] = (),
        assignee_id: str | None = None,
        delegate_id: str | None = None,
        clear_delegate: bool = False,
        priority: int | None = None,
        description: str | None = None,
    ) -> None: ...
    def attach_link(self, issue_id: str, url: str, title: str, *, run_id: str) -> Optional[str]: ...
```

**Write guards** (`WriteRefused`, checked inside the three write methods, not by callers):

1. `create_comment` calls `agency_os.gate.assert_not_gate_opening(body, author_is_agent=True)`.
2. `update_issue` refuses `poort/akkoord` and `poort/afgekeurd` in `added_labels`.
3. `update_issue` never builds a `labelIds` variable. Only `addedLabelIds` / `removedLabelIds`
   exist in `queries.ISSUE_UPDATE`. Spec §1.5.
4. `update_issue` refuses to leave a state whose current name starts with `Poort` unless the caller
   passes a `GateObservation` with `valid=True` through `gates.apply_gate_decision`
   (mechanically: the guard lives in `gates.py`, and `machine.assert_may_leave(state, obs)` is the
   single function that authorises it; `update_issue` requires `gate_ok=True` as a keyword when the
   issue's current state is a gate state).
5. `update_issue` refuses `removed_labels` containing `schakelaar/pauze-alles`. Spil may set the
   emergency stop, never clear it (roster §1).

In `dry_run=True` no HTTP write happens; the method returns `None` / a synthetic id and still emits
a `MutationRecord` with `dry_run=True`. This is what makes `dry-run` trustworthy.

### 3.4 `agency_os/linear/*` — remaining signatures, owned by A

```python
# poll.py
@dataclass(frozen=True)
class PollConfig:
    team_keys: tuple[str, ...]          # ("WV", "KR")
    panel_identifier: str               # "WV-156"
    in_scope_states: Mapping[str, tuple[str, ...]]   # team_key -> claimable state names
    max_claims: int                     # 4
def poll(client: LinearClient, cfg: PollConfig) -> PollResult: ...

# claim.py
def try_claim(client, store, issue: IssueView, run_id: str, *,
              settle_s: float = 5.0, now: Callable[[], datetime] = ...) -> Optional[Claim]: ...
def release_claim(client, store, claim: Claim, *, final_label: str) -> None: ...
    # final_label in {"run/klaar","run/mislukt","run/wachtrij","run/vastgelopen"}
def already_ran(store, issue_id: str, run_id: str) -> bool: ...
def existing_run_comment(client, issue_id: str, run_id: str) -> Optional[str]: ...

# machine.py
GATE_PREFIX = "Poort"
NEXT_ON_DONE: Mapping[tuple[str, str], str]
GATE_ON_APPROVE: Mapping[tuple[str, str], str]
GATE_ON_REJECT: Mapping[tuple[str, str], str]
WAIT_STATE: Mapping[str, str]            # team_key -> "Wacht op input"
def is_gate(state_name: str) -> bool: ...
def next_state(team_key: str, state_name: str, outcome: str) -> Optional[str]: ...
def assert_may_leave(state_name: str, obs: Optional[GateObservation]) -> None: ...  # raises WriteRefused

# gates.py
def evaluate_gate(client, issue: IssueView, *, approver_ids: frozenset[str],
                  dispatcher_user_id: str) -> GateObservation: ...
def enter_gate(client, issue: IssueView, *, run_id: str, gate_state: str, approver_id: str,
               card_markdown: str, artefact_url: Optional[str]) -> None: ...   # the six actions
def apply_gate_decision(client, store, issue: IssueView, obs: GateObservation, *,
                        run_id: str) -> Optional[str]: ...   # returns the new state name
def mark_unconfirmed(client, issue: IssueView, obs: GateObservation, *, run_id: str) -> None: ...

# killswitch.py
def read_switches(client, panel: Optional[IssueView], issues: Sequence[IssueView], *,
                  issue_count: int, thresholds: tuple[int, int, int]) -> SwitchState: ...
def halt_everything(client, store, switches: SwitchState, *, run_id: str) -> int: ...  # returns n aborted
def trip_emergency_stop(client, panel: IssueView, reason: str, *, run_id: str) -> None: ...

# comments.py
def signature(role_title: str, model_display: str, run_id: str, when: datetime) -> str: ...
def claim_comment(run_id: str, when: datetime) -> str: ...
def run_comment(*, role_title: str, model_display: str, run: RunRecord, body_md: str,
                evidence: Sequence[Artifact], dod: str, next_state: str) -> str: ...
def gate_card(*, gate_no: str, issue: IssueView, what: str, evidence: Sequence[Artifact],
              criteria: str, reviewers: str, disagreement: str, risk: str,
              cost_so_far: str, high_risk: bool, run_id: str, duration_s: float,
              cost_eur: float) -> str: ...
def qa_report(*, model_display: str, run_id: str, when: datetime, verdict: str, tested: str,
              criteria_rows: Sequence[tuple[str, str, str, str]], suite_ran: bool,
              suite_output: str, findings_rows: Sequence[tuple[str, str, str, str]],
              edge_cases: str, not_verified: str, regression_risk: str) -> str: ...
def halt_comment(run_id: str, when: datetime, aborted: int, elapsed_s: float, cost_eur: float) -> str: ...
def native_fallback_comment(agent_name: str, since: datetime, run_id: str) -> str: ...

# ledger.py
@dataclass(frozen=True)
class PriceRow:
    model: str; usd_in_per_mtok: float; usd_out_per_mtok: float; usd_cache_read_per_mtok: float
@dataclass(frozen=True)
class FxRate:
    usd_eur: float; source: str; on: date
@dataclass(frozen=True)
class DayRollup:
    day: date; runs: int; issues: int; usd: float; eur: float
    by_role: Mapping[str, float]; by_klant: Mapping[str, float]
    gates_passed: int; gates_rejected: int; median_gate_wait_s: Optional[float]
    supervision_minutes: float; first_pass_ok: tuple[int, int]; issue_count: int; loops: int
def record_run(store, run: RunRecord) -> None: ...
def render_tail_block(run: RunRecord) -> str: ...
def parse_tail_block(comment_body: str) -> Optional[RunRecord]: ...
def rollup(store, day: date) -> DayRollup: ...
def render_markdown(store, *, since: date, until: date, prices: Sequence[PriceRow],
                    fx: FxRate) -> str: ...       # the three sections of D05
```

### 3.5 `agency_os/executors/base.py` — owned by B

```python
@dataclass(frozen=True)
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    turns: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    source: str = "unknown"    # "claude-json" | "codex-cli" | "native-unmetered" | "unknown"
    metered: bool = True

@dataclass(frozen=True)
class ExecutorConfig:
    claude_bin: str = "claude"
    codex_bin: str = "codex"
    gh_bin: str = "gh"
    git_bin: str = "git"
    repo_root: Path = Path.home() / "Developer/Personal/Raderwerk"
    worktree_root: Path = Path.home() / "Developer/Personal/Raderwerk/.worktrees"
    allowed_repos: frozenset[str] = frozenset({          # public fictional repos only
        "raderwerk/agency-os", "raderwerk/raderwerk-content", "raderwerk/raderwerk-site",
        "raderwerk/kantelbeer-site", "raderwerk/spoorlinde-web",
        "raderwerk/zoutkaap-shop", "raderwerk/zoutkaap-erp-bridge", "raderwerk/zoutkaap-erp-mock",
    })
    forbidden_path_prefixes: tuple[str, ...] = ("/Users/youp/Developer/Fightclub",)
    run_timeout_s: int = 1800
    native_session_timeout_s: int = 3600
    codex_model: str = "gpt-5.6-sol"
    codex_reasoning_effort: str = "xhigh"
    cursor_model: str = "cursor-grok-4.6-high-fast"
    dry_run: bool = False

@dataclass(frozen=True)
class ExecutionRequest:
    run_id: str
    issue: IssueView
    role_key: str                 # "redacteur"
    role_title: str               # "Redacteur"
    model_key: str                # "sonnet" | "opus" | "fable" | "codex" | "cursor"
    model_display: str            # "Claude Sonnet 5"
    model_ledger: str             # "claude-sonnet-5"
    prompt: str                   # fully assembled by C; B never edits it
    repo: Optional[str]           # "raderwerk/raderwerk-content"
    base_branch: str              # "main"
    branch: str                   # "feat/WV-207-publiek-bouwlogboek"
    needs_worktree: bool
    needs_pr: bool
    pr_title: str
    pr_body: str
    timeout_s: int
    dry_run: bool

@dataclass(frozen=True)
class ExecutionResult:
    run_id: str
    uitkomst: str                 # "klaar" | "vraag" | "mislukt" | "afgebroken"
    summary_md: str               # Dutch prose, becomes the body of the signed comment
    dod: str                      # "6/6" | "-"
    question: Optional[str]       # set iff uitkomst == "vraag"
    error: Optional[str]          # set iff uitkomst in {"mislukt","afgebroken"}
    pr_url: Optional[str]
    branch: Optional[str]
    artifacts: tuple[Artifact, ...]
    usage: Usage
    started_at: datetime
    ended_at: datetime
    session_id: Optional[str]     # claude session id or Linear agentSession id
    raw_log_path: Optional[Path]  # stdout/stderr capture under the state dir

@dataclass(frozen=True)
class TriggerReceipt:
    run_id: str
    issue_id: str
    executor: str                 # "native-codex" | "native-cursor"
    trigger_comment_id: Optional[str]
    session_id: Optional[str]     # None until the session appears
    triggered_at: datetime
    strikes: int                  # consecutive awaitingInput/error/stale polls seen

class SyncExecutor(Protocol):
    name: str                     # "claude" | "codex-cli"
    def run(self, req: ExecutionRequest) -> ExecutionResult: ...

class AsyncExecutor(Protocol):
    name: str                     # "native-codex" | "native-cursor"
    def trigger(self, client, req: ExecutionRequest) -> TriggerReceipt: ...
    def poll(self, client, receipt: TriggerReceipt, issue: IssueView
             ) -> tuple[TriggerReceipt, Optional[ExecutionResult]]: ...

def build_executors(cfg: ExecutorConfig) -> dict[str, SyncExecutor | AsyncExecutor]: ...
def assert_safe_worktree(path: Path, repo: str, cfg: ExecutorConfig) -> None: ...
```

`assert_safe_worktree` raises `UnsafeWorktree` unless **all** hold: the resolved path is under
`cfg.worktree_root`; `repo in cfg.allowed_repos`; the resolved path does not start with any entry
of `forbidden_path_prefixes`. `claude_runner` calls it immediately before adding
`--dangerously-skip-permissions`, and that flag is added *only* when the call returns.

### 3.6 `agency_os/executors/*` — remaining signatures, owned by B

```python
# worktree.py
@dataclass(frozen=True)
class Worktree:
    repo: str; path: Path; branch: str; base: str; created: bool; head_sha: str
def slugify(title: str, *, max_words: int = 4) -> str: ...           # "Publiek bouwlogboek, wekelijks" -> "publiek-bouwlogboek"
def branch_name(identifier: str, title: str, *, prefix: str = "feat") -> str: ...
def find_existing_branch(cfg, repo: str, identifier: str) -> Optional[str]: ...   # probes feat/<ISSUE>-*
def ensure_worktree(cfg, repo: str, identifier: str, title: str, base: str) -> Worktree: ...
def remove_worktree(cfg, wt: Worktree, *, keep_branch: bool = True) -> None: ...

# gh.py
@dataclass(frozen=True)
class PullRequest:
    repo: str; number: int; url: str; state: str; is_draft: bool
    merged: bool; merged_by_login: Optional[str]; merged_by_is_bot: Optional[bool]
    head_sha: str; checks_conclusion: Optional[str]   # "success"|"failure"|"pending"|None
def find_pr_for_branch(cfg, repo: str, branch: str) -> Optional[PullRequest]: ...
def open_pr(cfg, repo: str, branch: str, base: str, title: str, body: str) -> PullRequest: ...
def read_pr(cfg, repo: str, number: int) -> PullRequest: ...
def pr_diff(cfg, repo: str, number: int, *, max_bytes: int = 400_000) -> str: ...

# claude_runner.py
class ClaudeRunner:                      # implements SyncExecutor
    name = "claude"
    def __init__(self, cfg: ExecutorConfig) -> None: ...
    def run(self, req: ExecutionRequest) -> ExecutionResult: ...
def parse_claude_json(stdout: str) -> tuple[str, Usage, Optional[str]]: ...  # (result_text, usage, session_id)
def parse_runresult(result_text: str) -> dict: ...      # the RUNRESULT block, {} when absent

# native.py
class NativeExecutor:                    # implements AsyncExecutor
    name: str
    def __init__(self, cfg: ExecutorConfig, agent: str) -> None: ...   # agent in {"codex","cursor"}
    def trigger(self, client, req) -> TriggerReceipt: ...
    def poll(self, client, receipt, issue) -> tuple[TriggerReceipt, Optional[ExecutionResult]]: ...
def mention_body(agent: str, req: ExecutionRequest) -> str: ...
def extract_pr_url(session: AgentSessionView) -> Optional[str]: ...

# codex_cli.py
class CodexCliReviewer:                  # implements SyncExecutor
    name = "codex-cli"
    def __init__(self, cfg: ExecutorConfig) -> None: ...
    def run(self, req: ExecutionRequest) -> ExecutionResult: ...

# cost.py
def normalise(usage: Usage, model_ledger: str, prices: Sequence[PriceRow]) -> Usage: ...
def to_eur(usd: float, fx: FxRate) -> float: ...
def budget_flag(total_eur_for_issue: float, threshold_eur: float = 10.0) -> bool: ...
```

### 3.7 `agency_os/app/*` — owned by C

```python
# config.py
@dataclass(frozen=True)
class Config:
    linear_api_key: str
    linear_api_key_source: str          # "env:SPIL_LINEAR_API_KEY" | "file:~/.config/..." — never the key
    linear_endpoint: str
    dispatcher_user_id: str
    approver_ids: frozenset[str]
    panel_identifier: str               # "WV-156"
    state_dir: Path                     # ~/.local/state/raderwerk
    db_path: Path                       # <state_dir>/spil.sqlite3
    logbook_dir: Path                   # <state_dir>/logbook
    interval_s: int
    max_claims_per_cycle: int
    max_concurrent_runs: int
    claim_settle_s: float
    heartbeat_every_cycles: int
    watchdog_max_age_s: int
    issue_budget: tuple[int, int, int]  # (200, 220, 225)
    fx: FxRate
    prices: tuple[PriceRow, ...]
    allow_fable: bool
    dry_run: bool
    executors: ExecutorConfig
    @staticmethod
    def load(argv_overrides: Mapping[str, str] | None = None) -> "Config": ...
    def redacted(self) -> dict: ...     # safe to print in `status`

# routing.py
@dataclass(frozen=True)
class RoleSpec:
    key: str; title: str; prompt_file: str
    default_model: str                  # "sonnet"
    family: str                         # "claude" | "openai" | "xai"
    executor: str                       # "claude" | "native" | "codex-cli"
    working_state: str                  # state to set while the run is in flight
    done_state: str                     # state on uitkomst=klaar
    needs_worktree: bool
    needs_pr: bool
    model_from_label: bool = True       # false for the judging roles: agent/* is the maker's model
    needs_evidence: bool = False        # true for the judging roles: they get the evidence block
@dataclass(frozen=True)
class Route:
    role: RoleSpec
    model_key: str                      # after agent/* override
    executor_name: str                  # after agent/codex|cursor override
    reason: str                         # human-readable, goes in the comment (spec ch. 3 rule)
def load_table(path: Path) -> "RoutingTable": ...
def resolve(table, issue: IssueView, *, allow_fable: bool) -> Optional[Route]: ...
MAX_ROLE_RUNS_PER_DAY = 3               # spec 8.6; mirrored by linear.store.LOOP_LIMIT
def loop_guard(store, issue: IssueView, role_key: str, day: date) -> Optional[str]: ...  # reason to stop

# prompts.py
def build_prompt(cfg: Config, role: RoleSpec, issue: IssueView, *, run_id: str,
                 branch: str = "", base_branch: str = "main",
                 discussion: Sequence[CommentView] = (),
                 extra_context: Mapping[str, str] | None = None) -> str: ...
def acceptance_criteria(description: str) -> list[str]: ...
def dod_items(description: str) -> list[str]: ...

# evidence.py — fills the extra_context hook for the judging roles
def for_role(cfg: ExecutorConfig, role: RoleSpec, issue: IssueView, *, branch: str,
             discussion: Sequence[CommentView] = ()) -> Mapping[str, str]: ...

# logbook.py
class Logbook:                          # also implements MutationSink
    def __init__(self, directory: Path) -> None: ...
    def record(self, m: MutationRecord) -> None: ...
    def write(self, kind: str, *, run_id: str | None, issue: str | None, payload: dict) -> None: ...
    def export(self, since: date, until: date) -> str: ...

# heartbeat.py
def due(cycle_index: int, every: int) -> bool: ...
def beat(client, store, cfg, panel: IssueView, *, run_id: str) -> None: ...
def watchdog(client, store, cfg) -> int: ...   # exit code: 0 alive, 1 stop tripped, 2 could not check

# scheduler.py
@dataclass(frozen=True)
class CycleReport:
    at: datetime; cycle: int; polled: int; claimed: int; finished: int
    gates_seen: int; gates_applied: int; halted: bool; errors: tuple[str, ...]
def run_cycle(ctx: "Context", cycle_index: int) -> CycleReport: ...
def run_loop(ctx: "Context", interval_s: int, *, stop: threading.Event) -> None: ...
```

### 3.8 Test fakes (owned by C, frozen on day 1)

`tests/fakes.py` exists before A and B write their first test.

```python
class FakeClient:
    """Implements the full LinearClient read+write surface in memory.

    - seeded from tests/fixtures/*.json (raw Linear response shapes, not IssueViews)
    - records every write as a MutationRecord in .mutations
    - `.fail_next(mutation_name, exc)` to inject failures
    """
    issues: dict[str, IssueView]
    comments: dict[str, list[CommentView]]
    sessions: dict[str, list[AgentSessionView]]
    mutations: list[MutationRecord]

def make_issue(**overrides) -> IssueView: ...      # sane WV-207-shaped default
def make_run(**overrides) -> RunRecord: ...
def make_session(**overrides) -> AgentSessionView: ...
def temp_store(tmpdir) -> "Store": ...             # a real sqlite Store on a temp file

class FakeExecutor:                                # SyncExecutor
    def __init__(self, result: ExecutionResult | Exception) -> None: ...
```

### 3.9 JSON shapes on the wire

**`agency_os/roles/routing.json`** — the routing table is *data*, per spec ch. 3.

```json
{
  "version": 1,
  "roles": {
    "redacteur": {
      "title": "Redacteur", "prompt_file": "redacteur.md", "default_model": "sonnet",
      "family": "claude", "executor": "claude", "working_state": "In uitvoering",
      "done_state": "Agentreview", "needs_worktree": true, "needs_pr": true
    },
    "reviewer": {
      "title": "Reviewer 1", "prompt_file": "reviewer.md", "default_model": "opus",
      "family": "claude", "executor": "claude", "working_state": "Agentreview",
      "done_state": "QA op preview", "needs_worktree": true, "needs_pr": false
    }
  },
  "rules": [
    {"team": "WV", "state": "Ingepland",
     "when": {"soort": ["contentstuk"]}, "role": "redacteur"},
    {"team": "WV", "state": "Ingepland",
     "when": {"dienst": ["web"], "soort": ["feature", "bug", "incident"]}, "role": "ontwikkelaar"},
    {"team": "WV", "state": "Ingepland",
     "when": {"soort": ["designtaak"]}, "role": "ontwerper"},
    {"team": "WV", "state": "Ingepland",
     "when": {"soort": ["campagne", "socialkalender"]}, "role": "campagneplanner"},
    {"team": "WV", "state": "Ingepland",
     "when": {"soort": ["bureau", "onderzoek"]}, "role": "ontwikkelaar"},
    {"team": "WV", "state": "Agentreview", "when": {}, "role": "reviewer"},
    {"team": "WV", "state": "QA op preview", "when": {}, "role": "qa"},
    {"team": "WV", "state": "Na-merge controle", "when": {}, "role": "qa-rookproef"},
    {"team": "KR", "state": "Lead", "when": {}, "role": "account"},
    {"team": "KR", "state": "Discovery", "when": {}, "role": "strateeg"},
    {"team": "KR", "state": "Voorstel", "when": {}, "role": "strateeg"}
  ]
}
```

Matching: first rule whose `team` and `state` match and whose every `when` key is satisfied
(`issue.label_in_group(key) in values`). Empty `when` matches anything. No match → skipped with
reason `geen-route`, logged, never claimed.

**RUNRESULT** — the structured tail every Claude/Codex role run must emit as the last fenced block
of its output. This is the only thing B parses from model prose; everything else is evidence.

````
```json RUNRESULT
{
  "uitkomst": "klaar",
  "samenvatting": "Vier weken bouwlogboek toegevoegd onder content/raderwerk/bouwlogboek/.",
  "dod": "6/6",
  "vraag": null,
  "pr_url": "https://github.com/raderwerk/raderwerk-content/pull/7",
  "bewijs": [
    {"type": "pr", "url": "https://github.com/raderwerk/raderwerk-content/pull/7", "label": "PR #7"},
    {"type": "test", "url": "https://github.com/raderwerk/raderwerk-content/actions/runs/123", "label": "npm run ci groen"}
  ]
}
```
````

Missing or unparseable block → `uitkomst="mislukt"`, `error="geen RUNRESULT-blok"`. No guessing
from prose, ever.

**Logbook line** — one JSON object per line, `<state_dir>/logbook/YYYY-MM-DD.jsonl`:

```json
{"at":"2026-09-03T11:14:52Z","kind":"mutation","run_id":"3f9a2c","issue":"WV-207",
 "payload":{"mutation":"issueUpdate","entity_id":"490eb350-...","result_id":"490eb350-...",
            "ok":true,"dry_run":false,"variables_summary":{"stateId":"<Agentreview>",
            "addedLabelIds":["run/klaar"],"removedLabelIds":["run/bezet"]},
            "variables_digest":"9f2c..."}}
```

`kind` ∈ `poll | claim | route | run | mutation | gate | heartbeat | halt | error | skip`.
The logbook is never committed: it lives under `~/.local/state/raderwerk/`, which is outside every
repo. `python -m agency_os ledger --logbook --since ... --until ...` prints it on demand.

**yaml tail block** — literally spec §8.3, rendered by `ledger.render_tail_block` and parsed back
by `ledger.parse_tail_block` (round-trip test is A's, mandatory). One extra key beyond the spec:
`gemeten: true|false`, false for native lanes, so the Kostenboek can state its own incompleteness
per row instead of only in a footnote.

---

## 4. Configuration

Read once at startup by `Config.load`, precedence **process env > `~/.config/raderwerk/spil.env` >
default**. The file is `KEY=VALUE`, `#` comments, no shell syntax, never committed, never printed.
`status` prints `linear_api_key_source` (e.g. `file:~/.config/raderwerk/spil.env`), never the key —
the same discipline `hq/tools/linear_api.py:key_source()` already uses, and for the same reason: an
exported key silently beating the file is how a run ends up pointed at the wrong workspace.

| Key | Default | Notes |
|---|---|---|
| `SPIL_LINEAR_API_KEY` | — | required; header is `Authorization: <key>`, no `Bearer` |
| `SPIL_LINEAR_ENDPOINT` | `https://api.linear.app/graphql` | |
| `SPIL_DISPATCHER_USER_ID` | — | required; the account Spil writes with. Gate condition 3 |
| `SPIL_APPROVER_IDS` | — | required; comma-separated Linear user uuids (D02 approver list) |
| `SPIL_PANEL_ISSUE` | `WV-156` | the Bedieningspaneel. Missing → heartbeat skipped, logged, no crash |
| `SPIL_STATE_DIR` | `~/.local/state/raderwerk` | sqlite + logbook + raw run logs |
| `SPIL_INTERVAL_S` | `60` | |
| `SPIL_MAX_CLAIMS_PER_CYCLE` | `4` | spec §8.2 |
| `SPIL_MAX_CONCURRENT_RUNS` | `2` | MVP; raise only after three clean dry runs |
| `SPIL_CLAIM_SETTLE_S` | `5` | spec §8.2 step 2 |
| `SPIL_RUN_TIMEOUT_S` | `1800` | per sync executor invocation |
| `SPIL_NATIVE_SESSION_TIMEOUT_S` | `3600` | native session deadline |
| `SPIL_HEARTBEAT_EVERY_CYCLES` | `15` | spec §8.4 |
| `SPIL_WATCHDOG_MAX_AGE_S` | `1800` | 30 minutes |
| `SPIL_ISSUE_BUDGET` | `200,220,225` | warn / restrict / stop |
| `SPIL_FX_USD_EUR`, `SPIL_FX_SOURCE`, `SPIL_FX_DATE` | — | **required**; no hardcoded rate (WV-161 AC) |
| `SPIL_PRICES` | built-in table | `model:in:out:cache` list; list prices, cliënt-side estimate |
| `SPIL_ALLOW_FABLE` | `false` | Fable quota is exhausted until 2026-09-05; see §18 |
| `SPIL_REPO_ROOT` / `SPIL_WORKTREE_ROOT` | `~/Developer/Personal/Raderwerk[/.worktrees]` | |
| `SPIL_CLAUDE_BIN` / `SPIL_CODEX_BIN` / `SPIL_GH_BIN` / `SPIL_GIT_BIN` | `claude`/`codex`/`gh`/`git` | |
| `SPIL_DRY_RUN` | `false` | |

Validation at startup, all failures fatal and named: missing key, unresolvable panel identifier,
unresolvable approver id, missing FX rate, `worktree_root` inside a forbidden prefix, a
`routing.json` role whose `prompt_file` does not exist.

---

## 5. State

### 5.1 SQLite — `~/.local/state/raderwerk/spil.sqlite3`

Owned by A (`store.py`). `PRAGMA journal_mode=WAL`, `foreign_keys=ON`. Schema version in `meta`;
migrations are forward-only numbered functions.

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE claims (                 -- the lock; one open row per issue
  issue_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  issue_identifier TEXT NOT NULL,
  role TEXT,
  claimed_at TEXT NOT NULL,
  released_at TEXT,
  outcome TEXT,
  PRIMARY KEY (issue_id, run_id)
);
CREATE UNIQUE INDEX one_open_claim_per_issue ON claims(issue_id) WHERE released_at IS NULL;

CREATE TABLE runs (                   -- the ledger
  run_id TEXT PRIMARY KEY,
  issue_id TEXT NOT NULL, issue_identifier TEXT NOT NULL, team_key TEXT NOT NULL,
  rol TEXT NOT NULL, model TEXT NOT NULL, executor TEXT NOT NULL,
  klant TEXT, dienst TEXT,
  gestart TEXT NOT NULL, geeindigd TEXT, duur_s REAL NOT NULL DEFAULT 0,
  beurten INTEGER NOT NULL DEFAULT 0,
  tokens_in INTEGER NOT NULL DEFAULT 0, tokens_uit INTEGER NOT NULL DEFAULT 0,
  cache_lees INTEGER NOT NULL DEFAULT 0,
  kosten_usd REAL NOT NULL DEFAULT 0, kosten_eur REAL NOT NULL DEFAULT 0,
  dod TEXT, uitkomst TEXT NOT NULL, volgende_status TEXT, pr_url TEXT,
  metered INTEGER NOT NULL DEFAULT 1,
  artefacten TEXT NOT NULL DEFAULT '[]'   -- json
);
CREATE INDEX runs_by_day ON runs(substr(gestart,1,10));
CREATE INDEX runs_by_issue ON runs(issue_id);

CREATE TABLE mutations (              -- handelingenlogboek, spec ch. 9 layer 3
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL, run_id TEXT, mutation TEXT NOT NULL, entity_id TEXT NOT NULL,
  variables_digest TEXT NOT NULL, variables_summary TEXT NOT NULL,
  result_id TEXT, ok INTEGER NOT NULL, error TEXT, dry_run INTEGER NOT NULL
);
CREATE INDEX mutations_by_entity ON mutations(entity_id, at);

CREATE TABLE gate_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id TEXT NOT NULL, gate_state TEXT NOT NULL,
  card_comment_id TEXT, card_at TEXT,
  decided_at TEXT, outcome TEXT, token TEXT, source TEXT, source_id TEXT,
  actor_id TEXT, actor_name TEXT, valid INTEGER NOT NULL, refusal TEXT,
  applied_at TEXT, rejections INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX gate_events_by_issue ON gate_events(issue_id, gate_state);

CREATE TABLE sessions (               -- native lane watcher
  issue_id TEXT NOT NULL, run_id TEXT NOT NULL, executor TEXT NOT NULL,
  session_id TEXT, trigger_comment_id TEXT, triggered_at TEXT NOT NULL,
  last_status TEXT, strikes INTEGER NOT NULL DEFAULT 0, closed_at TEXT,
  PRIMARY KEY (issue_id, run_id)
);

CREATE TABLE role_runs (              -- loop detection
  issue_id TEXT NOT NULL, role TEXT NOT NULL, day TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (issue_id, role, day)
);

CREATE TABLE heartbeats (
  at TEXT PRIMARY KEY, cycle INTEGER NOT NULL, comment_id TEXT, runs_today INTEGER,
  cost_eur_today REAL, queue_len INTEGER
);
```

`one_open_claim_per_issue` is the real concurrency guarantee inside this process. The Linear
`run/bezet` label is the cross-process signal, and the spec is honest that it is an approximation,
not a lock (§8.2). Both are used; neither is trusted alone.

### 5.2 JSONL logbook

`<state_dir>/logbook/YYYY-MM-DD.jsonl`, append-only, one line per event, shape in §3.9. Written by
C's `Logbook`, which is registered as a `MutationSink` on the client *and* called directly by the
scheduler for non-mutation events. Nothing under `state_dir` is ever added to a repo; `.gitignore`
already covers the repo, and the state dir is outside it anyway.

Raw executor output (claude stdout/stderr, codex output) goes to
`<state_dir>/runs/<run_id>/{stdout.json,stderr.txt}`, referenced from `ExecutionResult.raw_log_path`,
and is never posted to Linear in full.

---

## 6. One cycle, step by step

`scheduler.run_cycle(ctx, cycle_index)`. Steps map 1:1 onto spec §8.1.

```
 1  read: one batched GraphQL document (queries.POLL) covering
      - the panel issue (WV-156) with its labels
      - issues on WV and KR whose state.type not in (completed, canceled)
      - organization.createdIssueCount
 2  switches = killswitch.read_switches(...)
      global_pause  -> halt_everything(): stop in-flight runs, run/bezet -> run/wachtrij,
                       one abort comment per touched issue, one comment on the panel with
                       aborted count + elapsed since the flip + cost of the aborted runs,
                       then return a CycleReport(halted=True). Nothing else happens.
      budget_level  -> "warn": panel comment once a day
                       "restrict": only soort/incident is claimable
                       "stop": trip_emergency_stop() and ask a human for a decision
      engine_dead   -> log and continue (only the watchdog sets that label; it is informational
                       to the dispatcher, which by definition is alive if it is reading this)
 3  gates first (spec 8.1 step 5: gate decisions outrank everything)
      for each issue in poll.gates:
        obs = gates.evaluate_gate(...)
        obs.outcome is None                  -> nothing; the human has not answered yet
        obs.valid and outcome == "akkoord"   -> gates.apply_gate_decision(): confirmation comment,
                                                 poort/akkoord -> poort/vrij, remove wacht-op-mens,
                                                 next state per GATE_ON_APPROVE, clear the human
                                                 assignee, record supervision time (card->decision)
        obs.valid and outcome == "afgekeurd" -> reason quoted into an instruction comment,
                                                 back to GATE_ON_REJECT state, rejections += 1;
                                                 at rejections == 2: run/vastgelopen +
                                                 poort/wacht-op-mens + the three-choices comment,
                                                 and no third attempt
        not obs.valid                        -> gates.mark_unconfirmed(): run/onbevestigd +
                                                 schakelaar/mens-vereist + one comment naming which
                                                 of the five conditions failed. The issue is then
                                                 untouchable until a human clears it.
 4  claim: for each issue in poll.ready (already sorted: priority, then oldest updatedAt),
      skip when: run/bezet | schakelaar/pauze | run/vastgelopen | run/onbevestigd | agent/mens
                 | an open claim row exists | routing has no rule | loop_guard fires
      route = routing.resolve(table, issue, allow_fable=cfg.allow_fable)
      claim = claim.try_claim(...)          # label + comment + settle 5s + read-back
      stop at cfg.max_claims_per_cycle (4)
 5  execute: a ThreadPoolExecutor with cfg.max_concurrent_runs workers
      - state -> role.working_state before the executor starts (so the board is honest while it runs)
      - sync executors run to completion inside the cycle (the cycle may exceed its interval;
        the loop skips a tick rather than overlapping — see 6.1)
      - async (native) executors return a TriggerReceipt; the issue lands in poll.watching and is
        polled in later cycles by native.poll()
 6  write back, exactly three writes per run, in this order (spec 8.3):
      a. one comment: signature + prose + evidence + DoD + next state + yaml tail
      b. one issueUpdate: state, addedLabelIds / removedLabelIds, delegateId when relevant
      c. attachments for PR / preview when present
      outcome mapping: klaar -> next_state + run/klaar
                       vraag -> Wacht op input + schakelaar/mens-vereist + assign the human
                       mislukt -> stay + run/mislukt, failure counter += 1; at 2 on the same state
                                  it becomes a question for a human
                       afgebroken -> stay + run/wachtrij
      ledger.record_run(store, run) — always, including for failures
 7  entering a gate uses gates.enter_gate(), which performs the six actions of spec 7.2 in order.
 8  heartbeat: when heartbeat.due(cycle_index, 15) and the panel exists
 9  CycleReport -> logbook
```

### 6.1 Loop behaviour

`run --loop --interval 60` sleeps `max(0, interval - elapsed)`. If a cycle overran, the next one
starts immediately, and the overrun is logged. Cycles never overlap; there is exactly one
dispatcher process. `SIGINT`/`SIGTERM` set the stop event: in-flight sync runs are given
`min(30s, remaining timeout)` to finish, then their claims are released as `afgebroken` with
`run/wachtrij` so nothing stays locked. A crash without that grace leaves `run/bezet` in Linear and
an open claim row in sqlite; the next start reconciles by releasing open claims older than
`run_timeout_s` and setting them back to `run/wachtrij`, with a comment naming the restart.

---

## 7. State machine

`machine.py` holds these three tables as literal dicts. No `if` chains anywhere else in the
codebase decide a status.

```python
NEXT_ON_DONE = {
    ("WV", "Ingepland"):                    "In uitvoering",
    ("WV", "In uitvoering"):                "Agentreview",
    ("WV", "Agentreview"):                  "QA op preview",
    ("WV", "QA op preview"):                "Poort · Merge of publicatie",
    ("WV", "Na-merge controle"):            "Klaar",
    ("KR", "Lead"):                         "Gekwalificeerd",
    ("KR", "Discovery"):                    "Voorstel",
    ("KR", "Voorstel"):                     "Poort 1 · Voorstel akkoord",
}
GATE_ON_APPROVE = {
    ("WV", "Poort · Merge of publicatie"):  "Na-merge controle",
    ("KR", "Poort 1 · Voorstel akkoord"):   "Kickoff",
    ("KR", "Poort 2 · Oplevering akkoord"): "Poort 3 · Factuur akkoord",
    ("KR", "Poort 3 · Factuur akkoord"):    "Afgerond",
}
GATE_ON_REJECT = {
    ("WV", "Poort · Merge of publicatie"):  "In uitvoering",
    ("KR", "Poort 1 · Voorstel akkoord"):   "Voorstel",
    ("KR", "Poort 2 · Oplevering akkoord"): "Klantacceptatie",
    ("KR", "Poort 3 · Factuur akkoord"):    "Poort 2 · Oplevering akkoord",
}
```

Notes that matter:

* `Ingepland → In uitvoering` happens **at claim time**, not at run end, so the board never shows a
  claimed issue as still planned.
* `QA op preview → Poort · Merge of publicatie` goes through `gates.enter_gate`, never through a
  plain `update_issue`. That is where the six actions and the gate card live.
* `Na-merge controle` is entered by a human's approval, and its own run first **verifies** the merge
  through `gh.read_pr`: `merged == True` and `merged_by_is_bot == False` (spec §7.5). A merge by a
  bot account → `run/onbevestigd` + `schakelaar/mens-vereist`, no smoke test, no `Klaar`.
* KR `Voorstel → Poort 1` is a gate entry too. Poort 2 and Poort 3 have transition entries here so
  that a human decision on them is honoured, but the MVP has no role that *produces* the artefacts
  behind them; those issues will simply sit until a later milestone.
* `Wacht op input` is reachable from any state on outcome `vraag`, and the origin state is stored in
  `claims.role`/`runs.volgende_status` plus a machine-readable line in the comment, so a human's
  answer can send it back where it came from. Returning from `Wacht op input` is a human move in the
  MVP; Spil re-claims whatever state the human puts it in.

---

## 8. Gates

The gate is the whole point of the demo, so it gets the most paranoid code in the repo.

**Detection.** A gate state is any state whose name starts with `Poort`. `evaluate_gate` reads the
issue's comments newer than the gate card, plus the current `poort/*` label, and produces a
`GateObservation`. Token parsing is `agency_os.gate.parse_gate_decision`, which already exists and
already enforces the `AKKOORD RISICO-GEZIEN` rule for `risico/hoog`. A extends `gate.py` with one
function only:

```python
def strip_quotes_and_code(text: str) -> str:
    """Remove fenced blocks and '>' quote lines before token parsing (spec 7.8)."""
```

**The five conditions** (D02), all checked, each with its own refusal string:

1. `actor_id in cfg.approver_ids`
2. `actor_is_app is False`
3. `actor_id != cfg.dispatcher_user_id`
4. `decision_at > card_created_at` (strictly newer)
5. first line of `strip_quotes_and_code(body)` matches exactly `AKKOORD`, `AKKOORD RISICO-GEZIEN`,
   or `AFGEKEURD: <reden>`

A label flip to `poort/akkoord` / `poort/afgekeurd` is the second channel, and it is only as
authoritative as its actor. That actor comes from `Issue.history`. Verified against the live
workspace on 2026-09-03: `Issue.history` returns an empty node list, so the actor is *not*
available. Without an actor, conditions 1 to 4 cannot be checked at all, and "we did not set it
ourselves" only rules out this dispatcher — not a colleague, not a client guest, not a Codex or
Cursor session. The label channel therefore refuses when the actor is unknown: the observation is
invalid with `refusal=DEGRADED_ACTOR` and goes to `mark_unconfirmed`. Until `Issue.history` is
verified to carry an actor on this workspace, the comment channel is the only channel that can
open a gate.

The comment channel reads the **newest** decision strictly newer than the gate card, and refuses
outright when there is no card of ours on the issue. Reading the oldest decision first froze every
issue that visits a gate twice: round one's rejection became the answer to round two's card.

**Anything not valid stops the issue.** `mark_unconfirmed` sets `run/onbevestigd` +
`schakelaar/mens-vereist`, writes one comment saying exactly what it saw and which condition
failed, and the issue is excluded from `poll.ready` from then on. There is no "continue with a
note" path in the code. Not as a policy — as a missing branch.

**Spil can never open a gate.** Three independent guards: `create_comment` refuses a body whose
first line starts with a token; `update_issue` refuses `poort/akkoord`/`poort/afgekeurd` in
`added_labels`; `machine.assert_may_leave` refuses to leave a `Poort*` state without a valid
observation. Each has its own test. The gate card *contains* the token strings as instructions to
the human — they sit on their own lines mid-body, never on line 1, and the card's author is the
dispatcher, so even a copy of the card fails condition 3.

**Supervision measurement.** `card_at → decided_at` is the human supervision time (spec §7.5 step
8). It is stored on `gate_events` and rolled into `DayRollup.supervision_minutes`. It is one of the
two numbers that matter.

---

## 9. Routing and roles

The routing table is data (`roles/routing.json`, §3.9). `resolve()` applies, in order:

1. the first matching rule → `RoleSpec`
2. `agent/mens` on the issue → no route, ever
3. `agent/codex` or `agent/cursor` → executor becomes `native-codex` / `native-cursor`, the role's
   model becomes that agent, the prompt is not used (natives read Linear themselves; they get a
   dispatcher comment with repo, branch and the client-dossier link instead)
4. `agent/fable|opus|sonnet` → model override, winning over `default_model` (spec §3.6: an explicit
   label always wins). `agent/fable` with `allow_fable=false` falls back to `opus`, with the
   downgrade written into the comment — silent downgrades are worse than expensive ones.
5. estimate 5 (XL) → refuse, comment "XL is geen uitvoerbaar issue", back to Backlog (spec §2.3)
6. `dienst/web` without a resolvable `repo` → one question, `Wacht op input`, stop (spec §5)

**Role prompt** = D03 skeleton (`roles/_skelet.md`, the eight unbreakable rules verbatim) + the role
block (`roles/<role>.md`, lifted from `agent-roster.md`) + the issue block (identifier, title, URL,
description, canonical labels, parsed contract, acceptance criteria, DoD) + the target repo's
`AGENTS.md` read from the worktree + the output contract (§8.3 of the spec, plus the RUNRESULT
shape) + the discussion block (the issue's comments, oldest first, claim comments dropped, 12k
characters of budget spent on the newest) + whatever `extra_context` carries. Assembled by C in
`prompts.py`; B receives it as an opaque string and does not modify it.

**Evidence block** (`evidence.py`). The roles that judge rather than make — `needs_evidence` in
`routing.json`: reviewer, qa, qa-rookproef — tick criteria that are about artefacts, and they have
no `gh`: it is on `claude_runner.DENIED_TOOLS`, and the codex lane runs `-s read-only` with
`mcp_servers={}`. So the dispatcher looks the artefacts up and hands them over through the
`extra_context` hook: the PR number and URL with its state, the `gh pr checks` summary, the GitHub
Pages preview URL (marked "pas ná merge" whenever Pages publishes a branch other than this one, and
absent when the repo has no Pages), the branch and its HEAD sha, and a pointer to the latest
Reviewer/QA verdict per role — a pointer, because the text itself is already in the discussion
block and paying for it twice buys nothing. Every lookup degrades to a sentence rather than an
exception: a `gh` that cannot log in must not cost a run, and "niet op te halen" must never read as
green. With no PR the block says so and tells the role to report the criteria that need one as
*niet te verifiëren*.

**Loop detection.** `loop_guard(store, issue, role_key, day)` returns a reason once a role has had
`MAX_ROLE_RUNS_PER_DAY = 3` real runs on one issue in one day. That is the spec's threshold: §8.6
names three or more runs of the same role on the same issue in a day, and the `lus-verdacht` label
of §3.6 reads "dezelfde rol draaide vandaag drie keer op dit issue". The fourth is refused. Effect:
`lus-verdacht` + `schakelaar/pauze` on that issue, one comment, no claim.

*Real* runs. `role_runs` carries two numbers per (issue, role, day): `count`, every claim, and
`infra`, the claims where the lane itself never got off the ground — no worktree, no binary, a flag
the CLI does not know, a sandbox that refuses, a native session that never started. The guard reads
`count - infra`. `_claim` bumps `count` before the run, because a process that falls over halfway
must still lose its turn; `runs.finish` calls `discount_role_run` when the result carries
`ExecutionResult.infra_failure`. Executors set that flag in `base.failed()` (every "could not
start" path) and through `claude_runner.executor_stalled` (a non-zero exit code *and* no RUNRESULT
block). A timeout is not infrastructure: the model ran, it just ran too long. Neither is a model
that exits cleanly with unusable prose — that is a real, bad role outcome, and discounting it would
let one role produce prose forever without ever spending a turn.

Three a day is also the minimum that lets the ordinary loop close inside one day: build, get
rejected, repair. The earlier one-per-day rule made that impossible, and that is what cost WV-210
the afternoon of 2026-09-03 after a single reviewer run stranded on a CLI flag.

---

## 10. Executors

### 10.1 Claude role runner (`claude_runner.py`)

```
cd <worktree>
claude -p --output-format json --model <opus|sonnet|fable> \
       --dangerously-skip-permissions   # ONLY after assert_safe_worktree passes
```

`prompt` on stdin. `subprocess.run(..., timeout=req.timeout_s, start_new_session=True)`; on
timeout the whole process group is killed and the result is `afgebroken`. stdout is parsed as JSON:
`total_cost_usd`, `usage.input_tokens`, `usage.output_tokens`,
`usage.cache_read_input_tokens`, `num_turns`, `duration_ms`, `session_id`, `is_error`, `result`.
Every field is optional in the parser; a missing field yields zero and `Usage.source="unknown"`
rather than an exception. The `result` text is scanned for the RUNRESULT block.

Sequence for a maker role:

1. `ensure_worktree` — `git worktree add <root>/<repo-name>/<ISSUE> -b feat/<ISSUE>-<slug> origin/<base>`
   (fetch first; reuse the branch if `find_existing_branch` finds `feat/<ISSUE>-*` — this is the
   idempotency probe of spec §8.2 and WV-157's acceptance criterion)
2. run claude
3. if `needs_pr` and the worktree has commits ahead of base: `git push -u origin <branch>` then
   `gh pr create` (or reuse via `find_pr_for_branch` — a restarted run never opens a second PR)
4. build `ExecutionResult` with the PR url, the RUNRESULT evidence and the usage

The runner never merges, never pushes to `main`, never force-pushes. Those are not policies in the
prompt; `gh.py` has no merge function and `worktree.py` refuses a push whose target branch equals
the base branch.

### 10.2 Native trigger and session watcher (`native.py`)

`trigger()` posts the mention comment through A's client (verified working forms):

* Codex: `@Codex <opdracht> in raderwerk/<repo>` — one Codex cloud environment per repo is
  required; without it the delegation fails with "failed to start"
* Cursor: `@Cursor <opdracht> repo=raderwerk/<repo> branch=main`

and sets `delegateId` to the app user (board semantics; re-setting it does *not* start a session,
which is why the comment is the trigger). A `sessions` row is written.

`poll()` reads `issue.agentSessions.nodes { id status summary updatedAt appUser { id name app }
activities(first: 20) { nodes { id createdAt content } } pullRequests { nodes { url } } }`
(verified queryable 2026-09-03) and maps status:

| status | action |
|---|---|
| `pending`, `active` | strikes = 0, keep waiting until `native_session_timeout_s` |
| `complete` | extract the PR link from `pullRequests`, else from the summary/activities via `https://github\.com/raderwerk/[\w.-]+/pull/\d+`; `uitkomst="klaar"`; `Usage(metered=False, source="native-unmetered")` |
| `awaitingInput`, `error`, `stale` | strikes += 1. At **2 consecutive** polls: clear `delegateId`, post the literal fallback comment from roster §4, set `run/vastgelopen`, hand the issue back to the router for the Claude counterpart, and set `bewijs-ontbreekt` when this was reviewer 2 on a `risico/hoog` issue |

Never silently continue with one reviewer. That rule is a test, not a comment.

### 10.3 Codex CLI second opinion (`codex_cli.py`)

```
codex exec -m gpt-5.6-sol -c model_reasoning_effort=xhigh -c 'notify=[]' --search
```

run inside a read-only checkout of the PR head, with the reviewer prompt and the diff from
`gh.pr_diff`. This is the *second* reviewer, from a different model family than any Claude maker,
which is the roster's cheapest quality measure. Its output must also end in a RUNRESULT block; its
`uitkomst` maps to `klaar` (advice written) regardless of its verdict — the verdict itself lives in
the review comment and drives the transition in C.

Cost: the codex CLI does not report a USD figure the way `claude -p` does. If token counts are
present in its output they are recorded with `source="codex-cli"` and cost is computed from the
price table; otherwise `metered=False`. The Kostenboek says which rows are unmetered instead of
quietly averaging them in.

---

## 11. Review, second opinion, QA

**Agentreview** (state `Agentreview`, role `reviewer`):

* Reviewer 1 = Claude Opus 5 via `claude_runner`, on a read-only worktree at the PR head.
* Reviewer 2 = `codex exec` via `codex_cli`, in parallel — a two-worker `ThreadPoolExecutor`. They
  do not see each other's output; each gets only the diff, the acceptance criteria, the DoD and the
  test output.
* Neither may be from the same family as the maker. `routing.resolve` refuses a reviewer whose
  `family` equals the maker's recorded family (read from the last `runs` row for that issue) and
  swaps to the other lane, with the swap written in the comment.
* Two comments are posted (this is the one place where "one comment per run" yields to "one comment
  per *reviewer* run" — they are two separate runs with two run ids).
* Verdicts: `goedkeuren` / `goedkeuren met opmerkingen` → `QA op preview`. Any `blokkerend` finding
  from either reviewer → back to `In uitvoering` with the findings quoted as the instruction, and
  the repair counter incremented so first-pass acceptance stays measurable.
* Disagreement between the two is not resolved by Spil; it is carried verbatim into the gate card
  under **Oneens**, because that is the line the human is there to judge.

**QA op preview** (role `qa`): produces the QA-rapport comment in the exact template of spec §5.9 —
one row per acceptance criterion with outcome and an evidence link, the full test output, findings
by severity, edge cases, what could not be verified, and one explicit verdict. Rejection is
mandatory when the suite did not run, when a DoD item is ticked without evidence, or when a
criterion is "niet te verifiëren" and the issue did not allow that in advance. `bewijs-ontbreekt`
is set on the issue in that last case. On approval, `gates.enter_gate` writes the merge gate card.

**Na-merge controle**: `gh.read_pr` verification (§7), then a smoke run of the repo's CI command on
the merged main, then `Klaar`.

---

## 12. Cost ledger

One `runs` row per run, written unconditionally — including failures and aborts, because a ledger
that only records successes is a marketing document.

Per-run fields are exactly the yaml tail block of spec §8.3 plus `gemeten`. `ledger.render_markdown`
produces the three sections of D05:

1. **Rates and assumptions** — FX with source and date from config (never hardcoded), the list
   prices per million tokens, and the explicit sentence that these are client-side estimates on
   list price, not invoice data, plus the sentence that Codex and Cursor token usage falls outside
   this book and the unit economics are therefore structurally incomplete (WV-161 acceptance
   criteria, spec ch. 11).
2. **Run rows** — date, issue, role, model, turns, tokens in/out, cache read, USD, EUR, duration,
   outcome, metered yes/no.
3. **Daily close** — the exact block from spec ch. 11, including `supervisie` (from `gate_events`
   card→decision deltas) and `eerste-keer-goed` (issues that reached `Klaar` with zero rejections
   and zero repair runs, over issues that reached `Klaar`).

`python -m agency_os ledger --format markdown --since 2026-09-01` prints it. Posting it into the D05
document is a human action for now (`--post` is deliberately not implemented in the MVP: document
mutation is the one write surface we have not verified end to end).

`budget-let-op` is added to an issue whose cumulative model cost passes €10. It is information, not
a brake (spec §8.6).

---

## 13. Safety

| Risk | Mechanism | Where |
|---|---|---|
| Two runs on one issue | `run/bezet` label + 5s settle + read-back + lowest run id wins + sqlite unique open claim | A `claim.py` |
| Duplicate comment on restart | probe for a comment containing this run id before writing | A `claim.py`, `comments.py` |
| Duplicate PR on restart | probe `feat/<ISSUE>-*` and `find_pr_for_branch` before creating | B `worktree.py`, `gh.py` |
| Runaway concurrency | `max_concurrent_runs` (2) and `max_claims_per_cycle` (4) | C `scheduler.py` |
| Hanging model run | `subprocess` timeout + process-group kill → `afgebroken`, claim released | B `claude_runner.py` |
| Hanging native session | `native_session_timeout_s` + two-strike rule | B `native.py` |
| Agent opens a gate | three independent guards, each tested | A `client.py`, `gates.py`, `gate.py` |
| Label set wiped | `labelIds` does not exist in any mutation document | A `queries.py` |
| Emergency stop cleared by machine | `update_issue` refuses to remove `schakelaar/pauze-alles` | A `client.py` |
| Dispatcher dies silently | heartbeat every 15 cycles + `heartbeat --watchdog` in a separate cron process, which also treats a Linear 401 as death | C `heartbeat.py` |
| Issue budget exhausted | 200 warn / 220 incidents only / 225 emergency stop, read every cycle | A `killswitch.py` |
| Untracked mutation | every write emits a `MutationRecord` to sqlite *and* the JSONL logbook, before the caller sees the result | A `client.py` |
| Agent touches private work | `assert_safe_worktree` refuses anything outside the worktree root, outside the public-repo allowlist, or under `/Users/youp/Developer/Fightclub` | B `base.py` |
| Secrets in logs | `variables_summary` is an allowlist; the API key is never logged, only its source | A `client.py`, C `config.py` |

`dry-run` exercises the entire pipeline with writes suppressed at the client boundary and every
suppressed write printed as the mutation it would have been. It is the acceptance test for "does
Spil understand the board" without touching it.

---

## 14. CLI

```
python -m agency_os run --once [--issue WV-207] [--max-claims N]
python -m agency_os run --loop --interval 60
python -m agency_os status [--json]
python -m agency_os dry-run [--issue WV-207] [--cycles 1]
python -m agency_os heartbeat [--watchdog]
python -m agency_os ledger [--since D] [--until D] [--format markdown|json] [--logbook]
```

* `run --once` — exactly one cycle, then exit. `--issue` restricts the cycle to one issue, which is
  how the first live run is done.
* `run --loop` — the daemon. One process, handles SIGINT/SIGTERM as in §6.1.
* `status` — config source (never the key), workspace identity from `viewer`/`organization`, panel
  identifier and last heartbeat age, open claims, today's runs and cost, issue count against 250,
  kill-switch state, executor binaries found or missing. Exit code 1 when anything is unhealthy, so
  it composes with a shell check.
* `dry-run` — a full cycle with `dry_run=True`. Prints the routing decision and the exact intended
  mutations per issue.
* `heartbeat` — writes the heartbeat comment and refreshes the panel counters if due;
  `--watchdog` instead performs only the age check and, when the last heartbeat is older than
  `watchdog_max_age_s` (or the API answers 401), sets `schakelaar/motor-dood` on the panel and
  writes exactly one comment, and does nothing else ever. Cron entry:
  `*/10 * * * * python -m agency_os heartbeat --watchdog`.
* `ledger` — renders the Kostenboek markdown, or the raw logbook with `--logbook`.

Exit codes: `0` fine, `1` unhealthy/refused, `2` configuration error, `130` interrupted.

---

## 15. Tests

`python -m compileall -q agency_os tests && python -m unittest discover -s tests -v` stays the
whole build and the whole CI job. No new dependency, no network in any test.

| Area | Owner | Must cover |
|---|---|---|
| `test_gate.py` (exists) | A | plus: quoted/fenced token never counts; the three write guards each refuse |
| `test_linear_models.py` | A | canonical label names from leaf+parent; `Contract.parse` on all six WV templates incl. a missing block and an unknown key |
| `test_linear_machine.py` | A | every entry of the three tables; `assert_may_leave` refuses without a valid observation |
| `test_linear_gates.py` | A | each of the five conditions failing individually; second rejection → vastgelopen; high-risk bare AKKOORD refused; label channel without an actor refused; second gate round after a rejection |
| `test_linear_claim.py` | A | two claimers, lowest run id wins; restart writes no second comment; open-claim uniqueness |
| `test_linear_ledger.py` | A | tail-block round trip; roll-up sums equal the row sums (the manual checksum of WV-161); markdown contains the incompleteness sentence |
| `test_linear_killswitch.py` | A | pause-alles halts within one cycle; pause-alles cannot be removed; budget thresholds |
| `test_executors_worktree.py` | B | slug/branch naming; reuse of an existing `feat/<ISSUE>-*`; refuse a push to base |
| `test_executors_claude.py` | B | JSON parsing with missing fields; timeout → afgebroken; RUNRESULT present/absent; `--dangerously-skip-permissions` absent when the path is unsafe |
| `test_executors_native.py` | B | status mapping; two-strike fallback comment; PR-link extraction from three shapes |
| `test_executors_gh.py` | B | merged-by-bot detection; no merge function exists (`assertFalse(hasattr(gh,"merge"))`) |
| `test_app_routing.py` | C | every rule in routing.json; agent/* override; fable downgrade; XL refusal; loop guard on the second run |
| `test_app_config.py` | C | precedence env > file > default; missing FX is fatal; key never appears in `redacted()` |
| `test_app_scheduler.py` | C | full cycle against `FakeClient`: claim → run → three writes in order; gate-first ordering; halted cycle writes nothing but the halt |
| `test_app_heartbeat.py` | C | due-every-15; watchdog trips at >30 min and on 401; watchdog performs exactly one write |

Target: every guard in §13 has a test that fails when the guard is deleted.

---

## 16. Build order for three parallel engineers

Day 1, before anything else: **C writes `tests/fakes.py` and `agency_os/roles/routing.json`, and A
writes `agency_os/linear/models.py`**. Those three files are the contract surface; once they land on
`spil/design` (or an early PR onto `main`), A, B and C never need to talk again.

| | A (linear) | B (executors) | C (app) |
|---|---|---|---|
| 1 | `models.py`, `queries.py`, `client.py` + guards | `base.py`, `worktree.py` | `fakes.py`, `routing.json`, `config.py` |
| 2 | `store.py`, `claim.py` | `gh.py`, `claude_runner.py` | `prompts.py`, `roles/*.md` |
| 3 | `machine.py`, `gates.py`, `comments.py` | `native.py`, `codex_cli.py`, `cost.py` | `scheduler.py`, `logbook.py` |
| 4 | `poll.py`, `killswitch.py`, `ledger.py` | polish + tests | `cli.py`, `heartbeat.py`, README |

Each engineer opens their own PR per file group; every PR is reviewed by a role from another model
family, per AGENTS.md. Nobody merges.

---

## 17. The first live run

**Issue: WV-207 — "Publiek bouwlogboek, wekelijks"**
`https://linear.app/fightclub-techhub/issue/WV-207/publiek-bouwlogboek-wekelijks`

Why this one:

* Team WV, currently `Backlog`, estimate 2 (S), so it is inside the "an agent runs unattended
  reliably" band of spec §2.3.
* Labels: `dienst/content`, `soort/contentstuk`, `klant/raderwerk`, `agent/sonnet`,
  `repo/raderwerk/raderwerk-content`, `risico-publiek`. That routes to **Redacteur on Claude Sonnet
  5 through the `claude` executor** — a Claude role, not Codex, not Cursor, exactly as required.
* The deliverable is markdown files under `content/raderwerk/`. `raderwerk-content` publishes
  nothing by itself (its own README says so), so the worst possible outcome of a bad run is an ugly
  PR that a human does not merge.
* Its CI (`npm run lint && npm run test`) is fast and local, so the run can produce real evidence
  instead of a promise.
* `risico-publiek` means human final editing is mandatory before publication — which exercises the
  merge gate rather than bypassing it. Good: the first live run should hit a gate, not avoid one.
* It has a complete `## Opdrachtcontract` block (`repo: raderwerk/raderwerk-content`,
  `basisbranch: main`, `omgeving: preview`, `publiek: true`), so `Contract.parse` has a real target.

Procedure:

1. `python -m agency_os status` — green.
2. `python -m agency_os dry-run --issue WV-207` — read the printed routing decision and the intended
   mutations. Nothing is written.
3. A human moves WV-207 from `Backlog` to `Ingepland`.
4. `python -m agency_os run --once --issue WV-207` — one claim, one run, one PR, one signed comment,
   state `Agentreview`.
5. `run --once` again for the two reviewers, again for QA, and the merge gate card appears with the
   human as assignee and `delegateId` empty.
6. The human answers `AKKOORD` (or flips the label), merges the PR by hand on GitHub, and the next
   cycle verifies the merge, runs the smoke test and sets `Klaar`.
7. `python -m agency_os ledger --since today` — the run rows exist, the daily close shows supervision
   minutes and first-pass acceptance.

Only after that loop closes without manual repair does `run --loop` get switched on.

---

## 18. Deviations from the spec, and honest gaps

Named here rather than buried, because a design document that hides its own compromises is the
thing this whole project is arguing against.

1. **~~Loop detection is stricter than the spec.~~ Resolved 2026-09-03.** The MVP used to stop at
   the second run of a role on an issue in a day, where spec §8.6 allows three. Two live cycles
   showed the price: a review-and-repair round does not fit in a day, and a single reviewer run
   that stranded on a CLI flag cost WV-210 the rest of the afternoon. The guard is now at the
   spec's three, and runs where the lane never started do not count toward it. See §9.
2. **Fable is off by default.** The roster puts Fable 5.1 on Account, Strateeg, QA and Reviewer 1.
   The Fable quota is exhausted until 2026-09-05, so `SPIL_ALLOW_FABLE=false` downgrades those roles
   to Opus 5 and writes the downgrade into every affected comment. This weakens the "reviewer is
   always a different family" rule when the maker is also Claude — which is why reviewer 2
   (`codex exec`) is mandatory and not optional in the MVP.
3. **QA has no browser.** The roster gives QA browser tooling. Headless QA in the MVP verifies
   through the PR diff, the CI output and an HTTP GET of the preview URL (status + title) — all
   three now handed to it in the evidence block of §9, since 2026-09-03. Any acceptance criterion
   that genuinely needs a rendered page is reported as *niet te verifiëren* and sets
   `bewijs-ontbreekt`, which blocks `Klaar`. It does not get ticked.
4. **Branch prefix.** The launching context says `<ISSUE>-<slug>`; the spec, the roster and every
   repo's AGENTS.md say `feat/<ISSUE>-<korte-titel>`, and WV-157's acceptance criterion probes for
   `feat/<ISSUE>-*`. The MVP uses `feat/<ISSUE>-<slug>` so the idempotency probe and the repo
   conventions agree.
5. **Native lanes are unmetered.** Codex and Cursor bill inside their own plans. Their runs get a
   ledger row with `gemeten: false` and zero cost. The Kostenboek says so in section 1 and per row.
   The unit economics are structurally incomplete and the document says that out loud.
6. **`Issue.history` does not supply an actor.** Verified 2026-09-03 against the live workspace:
   `query IssueHistory` on WV-156 returns `history: { nodes: [] }`. The label channel therefore
   refuses instead of degrading to "must have been a human", and a gate can only be opened by a
   comment. WV-159's first acceptance criterion is what would re-open the label channel.
7. **`idmap.json` is not an input.** Verified 2026-09-03: at least one issue UUID in it does not
   resolve. Everything is resolved by identifier or name at startup and cached in sqlite for the
   life of the process.
8. **One process, no supervision of the supervisor.** The watchdog watches the dispatcher; nothing
   watches the watchdog. The spec accepts this provided it is stated in the honesty document (D12).
   It is stated here too.
9. **Poort 2 and Poort 3 are honoured but not fed.** The MVP recognises and applies human decisions
   on them; it has no PM, Klantstem or Finops role to produce the artefacts that make them
   meaningful. Those roles are the next milestone, not this one.
