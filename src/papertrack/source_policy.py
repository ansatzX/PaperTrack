"""Explicit policy for optional third-party journal metadata sources."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from typing import Mapping


class CrossrefMode(str, Enum):
    DISABLED = "disabled"
    METADATA = "metadata"


@dataclass(frozen=True)
class SourcePolicy:
    crossref_mode: CrossrefMode = CrossrefMode.DISABLED

    @classmethod
    def official_only(cls) -> "SourcePolicy":
        return cls(CrossrefMode.DISABLED)

    @classmethod
    def from_environment(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> "SourcePolicy":
        values = os.environ if env is None else env
        raw = values.get("PAPERTRACK_CROSSREF_MODE", "disabled").strip().lower()
        raw = raw or "disabled"
        try:
            return cls(CrossrefMode(raw))
        except ValueError as exc:
            raise RuntimeError(
                "PAPERTRACK_CROSSREF_MODE must be 'disabled' or 'metadata'"
            ) from exc

    @property
    def crossref_metadata_enabled(self) -> bool:
        return self.crossref_mode is CrossrefMode.METADATA

    @property
    def crossref_discovery_enabled(self) -> bool:
        return self.crossref_mode is CrossrefMode.METADATA
