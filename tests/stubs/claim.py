"""Stand-in voor `agency_os.linear.claim` (onderdeel A), contract 3.4 en spec 8.2."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

from agency_os.linear import comments
from agency_os.linear.models import Claim


def try_claim(client, store, issue, run_id: str, *, settle_s: float = 5.0,
              now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> Optional[Claim]:
    at = now()
    if not store.open_claim(issue.id, run_id, issue.identifier, None, at):
        return None
    comment_id = client.create_comment(issue.id, comments.claim_comment(run_id, at), run_id=run_id)
    client.update_issue(issue.id, run_id=run_id, added_labels=["run/bezet"], removed_labels=["run/wachtrij"])
    return Claim(run_id=run_id, issue_id=issue.id, issue_identifier=issue.identifier,
                 claimed_at=at, comment_id=comment_id)


def release_claim(client, store, claim: Claim, *, final_label: str) -> None:
    client.update_issue(claim.issue_id, run_id=claim.run_id,
                        added_labels=[final_label], removed_labels=["run/bezet"])
    store.close_claim(claim.issue_id, claim.run_id, final_label, datetime.now(timezone.utc))


def already_ran(store, issue_id: str, run_id: str) -> bool:
    return any(row["run_id"] == run_id for row in store.open_claims())


def existing_run_comment(client, issue_id: str, run_id: str) -> Optional[str]:
    for comment in client.comments(issue_id):
        if run_id in comment.body:
            return comment.id
    return None
