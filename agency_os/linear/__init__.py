"""Module A: alles wat met Linear praat en alles wat onthoudt.

Afhankelijkheden: alleen de standaardbibliotheek. Module B en C importeren uit
dit pakket; dit pakket importeert nooit uit hen (architectuur 2).

De publieke namen staan hieronder bij elkaar zodat B en C één importregel nodig
hebben en niet tegen de interne indeling aan hoeven te kijken.
"""

from __future__ import annotations

from .claim import already_ran, existing_run_comment, release_claim, try_claim
from .client import LinearClient, LinearError, MutationSink, WriteRefused
from .comments import (
    claim_comment,
    gate_card,
    halt_comment,
    qa_report,
    run_comment,
    signature,
)
from .gates import apply_gate_decision, enter_gate, evaluate_gate, mark_unconfirmed
from .killswitch import halt_everything, read_switches, trip_emergency_stop
from .ledger import (
    DayRollup,
    FxRate,
    PriceRow,
    parse_tail_block,
    record_run,
    render_markdown,
    render_tail_block,
    rollup,
)
from .machine import (
    GATE_ON_APPROVE,
    GATE_ON_REJECT,
    GATE_PREFIX,
    NEXT_ON_DONE,
    WAIT_STATE,
    assert_may_leave,
    is_gate,
    next_state,
)
from .models import (
    ActivityView,
    AgentSessionView,
    Artifact,
    Claim,
    CommentView,
    Contract,
    GateObservation,
    IssueView,
    MutationRecord,
    PollResult,
    RunRecord,
    SwitchState,
    canonical_label_name,
)
from . import poll  # submodule; `poll.poll(...)` is the function
from .poll import PollConfig
from .store import Store

__all__ = [
    # client
    "LinearClient", "LinearError", "MutationSink", "WriteRefused",
    # models
    "ActivityView", "AgentSessionView", "Artifact", "Claim", "CommentView", "Contract",
    "GateObservation", "IssueView", "MutationRecord", "PollResult", "RunRecord", "SwitchState",
    "canonical_label_name",
    # poll (de module, niet de functie: `poll.poll(...)`) + claim
    "PollConfig", "poll", "try_claim", "release_claim", "already_ran", "existing_run_comment",
    # machine
    "GATE_PREFIX", "NEXT_ON_DONE", "GATE_ON_APPROVE", "GATE_ON_REJECT", "WAIT_STATE",
    "is_gate", "next_state", "assert_may_leave",
    # gates
    "evaluate_gate", "enter_gate", "apply_gate_decision", "mark_unconfirmed",
    # killswitch
    "read_switches", "halt_everything", "trip_emergency_stop",
    # comments
    "signature", "claim_comment", "run_comment", "gate_card", "qa_report", "halt_comment",
    # store + ledger
    "Store", "record_run", "render_tail_block", "parse_tail_block", "rollup", "render_markdown",
    "PriceRow", "FxRate", "DayRollup",
]
