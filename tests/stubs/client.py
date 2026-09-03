"""Stand-in voor `agency_os.linear.client` (onderdeel A), contract 3.3.

Alleen de foutsoorten en het sink-protocol staan hier echt: de tests praten met
`tests.fakes.FakeClient`, niet met een echte HTTP-client.
"""

from __future__ import annotations

from typing import Protocol, Sequence


class MutationSink(Protocol):
    def record(self, m) -> None: ...


class LinearError(RuntimeError):
    """Een fout die de API teruggaf."""

    def __init__(self, message: str, errors: list[dict] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []

    def matches(self, *needles: str) -> bool:
        haystack = (str(self) + str(self.errors)).lower()
        return any(needle.lower() in haystack for needle in needles)


class WriteRefused(RuntimeError):
    """Een schrijfcontrole weigerde. Wordt nooit gevangen en opnieuw geprobeerd."""


class LinearClient:
    """Alleen aanwezig zodat imports werken; A levert de echte."""

    def __init__(self, api_key: str, *, endpoint: str = "", dispatcher_user_id: str = "",
                 sinks: Sequence[MutationSink] = (), dry_run: bool = False, timeout_s: int = 90) -> None:
        raise NotImplementedError("agency_os.linear.client is nog niet gebouwd (onderdeel A)")
