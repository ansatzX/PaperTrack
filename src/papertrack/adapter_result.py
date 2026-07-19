"""Structured source adapter results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")

SUCCESS = "success"
PARTIAL = "partial"
BLOCKED = "blocked"
RATE_LIMITED = "rate_limited"
FETCH_FAILED = "fetch_failed"
CIRCUIT_OPEN = "circuit_open"
PARSE_FAILED = "parse_failed"
OFFICIAL_EMPTY_SUSPICIOUS = "official_empty_suspicious"
OFFICIAL_FIELD_ABSENT = "official_field_absent"


@dataclass
class AdapterResult(Generic[T]):
    entries: T
    outcome: str
    provider: str
    source: str
    source_role: str = "authoritative"
    message: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def has_entries(self) -> bool:
        return bool(self.entries)

    @property
    def ok(self) -> bool:
        return self.outcome in {SUCCESS, PARTIAL}
