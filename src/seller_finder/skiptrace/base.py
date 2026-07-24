"""Skip-trace provider interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SkipTraceResult:
    """Normalized skip-trace result, provider-agnostic."""
    matched: bool = False
    emails: list[str] = field(default_factory=list)
    phones: list[dict] = field(default_factory=list)  # {number, type, score, reachable}
    dnc: bool = False
    litigator: bool = False
    raw: dict = field(default_factory=dict)
    provider: str = ""


@dataclass
class SkipTraceRequest:
    """One property/owner to trace."""
    street: str
    city: str
    state: str
    zip: str
    owner_first: str = ""
    owner_last: str = ""


class SkipTraceProvider(ABC):
    """Implement trace_batch; the pipeline handles caching and budgets."""

    name: str = "base"

    @abstractmethod
    def trace_batch(self, requests_: list[SkipTraceRequest]) -> list[SkipTraceResult]:
        """Trace up to provider-defined batch size; results align with input order."""
        raise NotImplementedError
