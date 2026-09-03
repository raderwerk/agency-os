"""GraphQL-transport naar Linear, plus de schrijfsloten.

Elke schrijfactie van het hele systeem gaat door dit bestand. Dat is precies
waarom "elke mutatie staat in het logboek" en de drie poortsloten hier
afdwingbaar zijn en niet bij elke aanroeper opnieuw.

Auth: `Authorization: <key>`, **geen** `Bearer` (persoonlijke sleutel).
Leesacties worden opnieuw geprobeerd bij 429 en 5xx; schrijfacties alleen bij
429, want daar staat vast dat de mutatie niet gedraaid heeft. Een opnieuw
verstuurde `commentCreate` na een verloren antwoord zou een dubbele comment
opleveren, en dubbel schrijven is erger dan één keer falen.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Protocol, Sequence

from .. import gate
from . import queries
from .models import (
    ActivityView,
    AgentSessionView,
    CommentView,
    IssueView,
    MutationRecord,
    canonical_label_name,
    issue_from_node,
    parse_dt,
)

__all__ = ["MutationSink", "LinearError", "WriteRefused", "LinearClient"]

USER_AGENT = "raderwerk-spil/0.1"
MAX_ATTEMPTS = 5
BASE_BACKOFF = 2.0
MAX_BACKOFF = 60.0
REQUEST_FLOOR = 40
COMPLEXITY_FLOOR = 20_000
RESET_WAIT_CAP = 3900.0

FORBIDDEN_GATE_LABELS = ("poort/akkoord", "poort/afgekeurd")
EMERGENCY_STOP_LABEL = "schakelaar/pauze-alles"

# Wat er van een mutatie in het logboek terechtkomt. Een allowlist, geen
# blocklist: `body` en `description` mogen er nooit per ongeluk bij glippen.
SUMMARY_KEYS = (
    "stateId", "addedLabelIds", "removedLabelIds", "assigneeId", "delegateId",
    "priority", "issueId", "url", "title",
)


class MutationSink(Protocol):
    """Iets dat een MutationRecord wegschrijft (de sqlite-store, het logboek)."""

    def record(self, m: MutationRecord) -> None: ...


class LinearError(RuntimeError):
    """Een fout van de API zelf (GraphQL-errors, HTTP, netwerk)."""

    def __init__(self, message: str, errors: Optional[list[dict]] = None) -> None:
        super().__init__(message)
        self.errors: list[dict] = errors or []

    @property
    def codes(self) -> list[str]:
        out: list[str] = []
        for err in self.errors:
            ext = err.get("extensions") or {}
            for key in ("code", "type", "userPresentableMessage"):
                if ext.get(key):
                    out.append(str(ext[key]))
        return out

    def matches(self, *needles: str) -> bool:
        hay = (str(self) + " " + " ".join(self.codes)).lower()
        return any(n.lower() in hay for n in needles)


class WriteRefused(RuntimeError):
    """Een schrijfslot heeft geweigerd.

    Wordt nooit gevangen en opnieuw geprobeerd: het betekent dat de code fout
    is, niet dat de API het even niet deed.
    """


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_parse_dt = parse_dt  # tijdstempels worden in models omgezet, hier alleen gebruikt


def _digest(variables: Mapping[str, Any]) -> str:
    blob = json.dumps(variables, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class LinearClient:
    """Leest en schrijft Linear. De enige plek in het systeem die dat mag."""

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = "https://api.linear.app/graphql",
        dispatcher_user_id: str,
        sinks: Sequence[MutationSink] = (),
        dry_run: bool = False,
        timeout_s: int = 90,
    ) -> None:
        self._api_key = api_key
        self.endpoint = endpoint
        self.dispatcher_user_id = dispatcher_user_id
        self.sinks = tuple(sinks)
        self.dry_run = dry_run
        self.timeout_s = timeout_s
        self._label_ids: Optional[dict[str, str]] = None
        self._state_ids: dict[str, dict[str, str]] = {}
        self._last_headers: dict[str, str] = {}
        self._dry_counter = 0

    # ---------------- transport ----------------

    def _post(self, document: str, variables: Mapping[str, Any]) -> str:
        """Eén HTTP-POST. Apart zodat een test hem kan vervangen zonder netwerk."""
        payload = json.dumps({"query": document, "variables": dict(variables)}).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": self._api_key,
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            self._last_headers = {k.lower(): v for k, v in response.headers.items()}
            return response.read().decode("utf-8")

    def _respect_budget(self) -> None:
        """Wachten zodra de rate-limitkoppen zeggen dat het budget bijna op is."""
        for remaining_key, reset_key, floor in (
            ("x-ratelimit-requests-remaining", "x-ratelimit-requests-reset", REQUEST_FLOOR),
            ("x-ratelimit-complexity-remaining", "x-ratelimit-complexity-reset", COMPLEXITY_FLOOR),
        ):
            raw = self._last_headers.get(remaining_key)
            if raw is None:
                continue
            try:
                if float(raw) >= floor:
                    continue
                reset_ms = float(self._last_headers.get(reset_key) or 0)
            except ValueError:
                continue
            wait = max(0.0, reset_ms / 1000.0 - time.time()) + 1.0
            time.sleep(min(wait, RESET_WAIT_CAP))
            return

    def _execute(self, document: str, variables: Mapping[str, Any], *,
                 retry_transport: bool) -> dict:
        self._respect_budget()
        last_error: Optional[BaseException] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            status: Optional[int] = None
            body: Optional[str] = None
            try:
                body = self._post(document, variables)
                status = 200
            except urllib.error.HTTPError as exc:
                status = exc.code
                self._last_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                try:
                    body = exc.read().decode("utf-8")
                except Exception:  # pragma: no cover - kapotte foutstroom
                    body = None
                last_error = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                status, body = None, None

            transport_failure = status is None or status >= 500
            retryable = status == 429 or (transport_failure and retry_transport)
            if retryable and attempt < MAX_ATTEMPTS:
                wait = min(MAX_BACKOFF, BASE_BACKOFF * (2 ** (attempt - 1)))
                time.sleep(wait + random.uniform(0, 0.5 * wait))
                continue
            if body is None:
                raise LinearError(
                    f"netwerkfout na {attempt} pogingen (de mutatie kan wel of niet geland "
                    f"zijn; opnieuw draaien en laten verzoenen): {last_error}"
                )
            try:
                parsed = json.loads(body)
            except ValueError:
                raise LinearError(f"HTTP {status}: geen JSON in het antwoord: {body[:300]}")
            if parsed.get("errors"):
                message = "; ".join(str(e.get("message", e)) for e in parsed["errors"])
                raise LinearError(message, errors=parsed["errors"])
            if status is not None and status >= 400:
                raise LinearError(f"HTTP {status}: {body[:300]}")
            if "data" not in parsed:
                raise LinearError(f"HTTP {status}: antwoord zonder data: {body[:300]}")
            return parsed["data"]
        raise LinearError(f"onbereikbaar: {last_error}")  # pragma: no cover

    # ---------------- reads ----------------

    def query(self, document: str, variables: Optional[dict] = None) -> dict:
        """Eén leesoperatie; geeft het `data`-object terug."""
        return self._execute(document, variables or {}, retry_transport=True)

    def paginate(self, document: str, path: str, variables: Optional[dict] = None,
                 page_size: int = 50) -> list[dict]:
        """Alle nodes van een connectie op `path` (puntpad in het data-object)."""
        nodes: list[dict] = []
        cursor: Optional[str] = None
        while True:
            payload = dict(variables or {})
            payload.update({"first": page_size, "after": cursor})
            data = self.query(document, payload)
            connection: Any = data
            for step in path.split("."):
                connection = (connection or {}).get(step) or {}
            nodes.extend(connection.get("nodes") or [])
            page = connection.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return nodes
            cursor = page.get("endCursor")

    def issue(self, identifier_or_id: str) -> IssueView:
        """Eén issue, op identifier ("WV-207") of uuid. Nooit op idmap.json."""
        data = self.query(queries.ISSUE_BY_ID, {"id": identifier_or_id})
        node = data.get("issue")
        if not node:
            raise LinearError(f"issue {identifier_or_id!r} bestaat niet")
        return self.to_issue_view(node)

    def comments(self, issue_id: str, *, limit: int = 50) -> list[CommentView]:
        data = self.query(queries.ISSUE_COMMENTS, {"id": issue_id, "first": limit})
        nodes = (((data.get("issue") or {}).get("comments") or {}).get("nodes")) or []
        return [self.to_comment_view(node) for node in nodes]

    def agent_sessions(self, issue_id: str) -> list[AgentSessionView]:
        data = self.query(queries.AGENT_SESSIONS, {"id": issue_id})
        nodes = (((data.get("issue") or {}).get("agentSessions") or {}).get("nodes")) or []
        return [self.to_session_view(node) for node in nodes]

    def issue_history(self, issue_id: str, *, limit: int = 50) -> Optional[list[dict]]:
        """Ruwe historie, of None als het veld niet bruikbaar is.

        `Issue.history` is onbevestigd (architectuur 18.6). None betekent
        "niet vast te stellen"; `gates.py` degradeert dan expliciet en logt dat.
        """
        try:
            data = self.query(queries.ISSUE_HISTORY, {"id": issue_id, "first": limit})
        except LinearError:
            return None
        return (((data.get("issue") or {}).get("history") or {}).get("nodes")) or []

    def label_ids(self) -> Mapping[str, str]:
        """Canonieke labelnaam -> uuid, één keer opgehaald per proces."""
        if self._label_ids is None:
            nodes = self.paginate(queries.LABELS, "issueLabels")
            self._label_ids = {
                canonical_label_name(n["name"], (n.get("parent") or {}).get("name")): n["id"]
                for n in nodes
            }
        return dict(self._label_ids)

    def state_ids(self, team_key: str) -> Mapping[str, str]:
        """Statusnaam -> uuid voor één team, gecachet."""
        if team_key not in self._state_ids:
            nodes = self.paginate(queries.WORKFLOW_STATES, "workflowStates", {"teamKey": team_key})
            self._state_ids[team_key] = {n["name"]: n["id"] for n in nodes}
        return dict(self._state_ids[team_key])

    def organization_issue_count(self) -> int:
        data = self.query(queries.ORGANIZATION, {})
        return int((data.get("organization") or {}).get("createdIssueCount") or 0)

    # ---------------- mapping ----------------

    def to_issue_view(self, node: Mapping[str, Any]) -> IssueView:
        """Ruwe Linear-json -> IssueView. De vormverandering staat in models."""
        return issue_from_node(node)

    def to_comment_view(self, node: Mapping[str, Any]) -> CommentView:
        user = node.get("user") or {}
        return CommentView(
            id=node["id"],
            body=node.get("body") or "",
            created_at=_parse_dt(node.get("createdAt")),
            author_id=user.get("id") or "",
            author_name=user.get("name") or "",
            author_is_app=bool(user.get("app")),
        )

    def to_session_view(self, node: Mapping[str, Any]) -> AgentSessionView:
        app_user = node.get("appUser") or {}
        activities = tuple(
            ActivityView(
                id=a.get("id") or "",
                type=str((a.get("content") or {}).get("type") or "response"),
                body=_activity_body(a.get("content") or {}),
                created_at=_parse_dt(a.get("createdAt")),
            )
            for a in ((node.get("activities") or {}).get("nodes") or [])
        )
        pull_requests = ((node.get("pullRequests") or {}).get("nodes") or [])
        return AgentSessionView(
            id=node["id"],
            status=node.get("status") or "pending",
            summary=node.get("summary"),
            app_user_id=app_user.get("id") or "",
            app_user_name=app_user.get("name") or "",
            created_at=_parse_dt(node.get("createdAt")),
            updated_at=_parse_dt(node.get("updatedAt")),
            activities=activities,
            pull_request_url=_first_pr_url(pull_requests),
        )

    # ---------------- writes ----------------

    def _emit(self, record: MutationRecord) -> None:
        failures: list[str] = []
        for sink in self.sinks:
            try:
                sink.record(record)
            except Exception as exc:  # pragma: no cover - defect in een sink
                failures.append(f"{type(sink).__name__}: {exc}")
        if failures:
            raise RuntimeError("mutatie niet volledig gelogd: " + "; ".join(failures))

    def _write(self, document: str, variables: Mapping[str, Any], *, run_id: Optional[str],
               mutation: str, entity_id: str, summary: Mapping[str, Any],
               result_path: Sequence[str]) -> Optional[str]:
        """Voert één mutatie uit en logt hem altijd, ook als hij faalt."""
        if self.dry_run:
            self._dry_counter += 1
            synthetic = f"dry-{run_id or 'x'}-{self._dry_counter}"
            self._emit(MutationRecord(
                at=_utcnow(), run_id=run_id, mutation=mutation, entity_id=entity_id,
                variables_digest=_digest(variables), variables_summary=dict(summary),
                result_id=synthetic, ok=True, error=None, dry_run=True,
            ))
            return synthetic
        try:
            data = self._execute(document, variables, retry_transport=False)
        except LinearError as exc:
            self._emit(MutationRecord(
                at=_utcnow(), run_id=run_id, mutation=mutation, entity_id=entity_id,
                variables_digest=_digest(variables), variables_summary=dict(summary),
                result_id=None, ok=False, error=str(exc), dry_run=False,
            ))
            raise
        result: Any = data
        for step in result_path:
            result = (result or {}).get(step) or {}
        result_id = result.get("id") if isinstance(result, dict) else None
        self._emit(MutationRecord(
            at=_utcnow(), run_id=run_id, mutation=mutation, entity_id=entity_id,
            variables_digest=_digest(variables), variables_summary=dict(summary),
            result_id=result_id, ok=True, error=None, dry_run=False,
        ))
        return result_id

    def create_comment(self, issue_id: str, body: str, *, run_id: str) -> Optional[str]:
        """Slot 1: een agent mag nooit een poortopenend comment schrijven."""
        try:
            gate.assert_not_gate_opening(body, author_is_agent=True)
        except gate.InvalidGateToken as exc:
            raise WriteRefused(str(exc)) from exc
        variables = {"input": {"issueId": issue_id, "body": body}}
        return self._write(
            queries.COMMENT_CREATE, variables, run_id=run_id, mutation="commentCreate",
            entity_id=issue_id, summary={"issueId": issue_id, "body_len": len(body)},
            result_path=("commentCreate", "comment"),
        )

    def update_issue(
        self,
        issue_id: str,
        *,
        run_id: str,
        state: Optional[str] = None,
        added_labels: Sequence[str] = (),
        removed_labels: Sequence[str] = (),
        assignee_id: Optional[str] = None,
        delegate_id: Optional[str] = None,
        clear_delegate: bool = False,
        priority: Optional[int] = None,
        description: Optional[str] = None,
        gate_ok: bool = False,
        clear_assignee: bool = False,
        current_state: Optional[str] = None,
        team_key: Optional[str] = None,
    ) -> None:
        """Eén `issueUpdate`. Vier van de vijf schrijfsloten zitten hierin.

        `gate_ok` wordt alleen door `gates.apply_gate_decision` gezet, en die
        vraagt eerst `machine.assert_may_leave` om toestemming. `current_state`
        en `team_key` mogen meegegeven worden om een extra leesronde te sparen.
        """
        for label in added_labels:
            if label in FORBIDDEN_GATE_LABELS:
                raise WriteRefused(
                    f"slot 2: de Spil mag {label!r} nooit zelf zetten; een poort gaat alleen "
                    "open door een mens"
                )
        if EMERGENCY_STOP_LABEL in removed_labels:
            raise WriteRefused(
                f"slot 5: de Spil mag {EMERGENCY_STOP_LABEL!r} wel aanzetten, nooit weghalen"
            )

        if state is not None and (current_state is None or team_key is None):
            context = self.query(
                "query IssueContext($id: String!) { issue(id: $id) "
                "{ id team { key } state { name } } }",
                {"id": issue_id},
            )
            node = context.get("issue") or {}
            current_state = current_state or (node.get("state") or {}).get("name") or ""
            team_key = team_key or (node.get("team") or {}).get("key") or ""
        if state is not None and (current_state or "").startswith("Poort") and not gate_ok:
            raise WriteRefused(
                f"slot 4: {current_state!r} is een poortstatus; verlaten mag alleen via "
                "gates.apply_gate_decision met een geldige GateObservation"
            )

        labels = self.label_ids() if (added_labels or removed_labels) else {}
        payload: dict[str, Any] = {}
        summary: dict[str, Any] = {}
        if state is not None:
            states = self.state_ids(team_key or "")
            if state not in states:
                raise WriteRefused(f"status {state!r} bestaat niet op team {team_key!r}")
            payload["stateId"] = states[state]
            summary["stateId"] = f"<{state}>"
        if added_labels:
            payload["addedLabelIds"] = [_label_id(labels, name) for name in added_labels]
            summary["addedLabelIds"] = list(added_labels)
        if removed_labels:
            payload["removedLabelIds"] = [_label_id(labels, name) for name in removed_labels]
            summary["removedLabelIds"] = list(removed_labels)
        if clear_assignee:
            payload["assigneeId"] = None
            summary["assigneeId"] = None
        elif assignee_id is not None:
            payload["assigneeId"] = assignee_id
            summary["assigneeId"] = assignee_id
        if clear_delegate:
            payload["delegateId"] = None
            summary["delegateId"] = None
        elif delegate_id is not None:
            payload["delegateId"] = delegate_id
            summary["delegateId"] = delegate_id
        if priority is not None:
            payload["priority"] = int(priority)
            summary["priority"] = int(priority)
        if description is not None:
            payload["description"] = description
            summary["description_len"] = len(description)
        if not payload:
            return
        # Slot 3: `labelIds` vervangt de hele labelset en bestaat daarom nergens.
        # Geen assert: die valt weg onder `python -O` en dit slot moet altijd staan.
        if "labelIds" in payload:  # pragma: no cover - alleen bereikbaar na een codefout
            raise WriteRefused("slot 3: labelIds vervangt de hele labelset en bestaat hier niet")
        self._write(
            queries.ISSUE_UPDATE, {"id": issue_id, "input": payload}, run_id=run_id,
            mutation="issueUpdate", entity_id=issue_id,
            summary={k: v for k, v in summary.items() if k in SUMMARY_KEYS or k.endswith("_len")},
            result_path=("issueUpdate", "issue"),
        )

    def attach_link(self, issue_id: str, url: str, title: str, *, run_id: str) -> Optional[str]:
        """Koppelt een artefact (PR, preview, document) aan het issue."""
        variables = {"issueId": issue_id, "url": url, "title": title}
        return self._write(
            queries.ATTACHMENT_LINK_URL, variables, run_id=run_id, mutation="attachmentLinkURL",
            entity_id=issue_id, summary={"issueId": issue_id, "url": url, "title": title},
            result_path=("attachmentLinkURL", "attachment"),
        )


def _activity_body(content: Mapping[str, Any]) -> str:
    """De leesbare tekst van één activiteit, welke variant van de union het ook is.

    Vijf van de zes dragen `body`; `AgentActivityActionContent` draagt `result`
    en `action`. `extract_pr_url` zoekt in deze tekst naar de PR-link, dus een
    actie die de PR opende mag hier niet leeg uit vallen.
    """
    for key in ("body", "result", "action"):
        value = content.get(key)
        if value:
            return str(value)
    return ""


def _first_pr_url(links: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """De eerste PR-url uit `agentSession.pullRequests`.

    De koppeltabel `AgentSessionToPullRequest` draagt zelf geen `url`; die zit
    een niveau dieper op `pullRequest`.
    """
    for link in links:
        url = ((link.get("pullRequest") or {}).get("url")) or link.get("url")
        if url:
            return str(url)
    return None


def _label_id(labels: Mapping[str, str], name: str) -> str:
    try:
        return labels[name]
    except KeyError:
        raise WriteRefused(
            f"label {name!r} bestaat niet in deze workspace; namen zijn canoniek "
            "(groep/blad) en worden nooit geraden"
        ) from None
