"""Typed result objects for the SDK.

Light dataclasses — no schema validation, no Pydantic dependency.
Server returns dicts; we wrap them in typed views for IDE-friendly access
without forcing callers to use them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MemoryEntry:
    """A single memory row returned by recall."""

    content: str
    rrf_score: float = 0.0
    vec_distance: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryEntry":
        return cls(
            content=str(d.get("content", "")),
            rrf_score=float(d.get("rrf_score", 0.0)),
            vec_distance=d.get("vec_distance"),
            raw=d,
        )


@dataclass
class RecallResult:
    """Result of a recall call.

    Holds both hot and cold matches as the server returns them. Provides
    convenience filters used by hooks (score threshold, distance gate).
    """

    hot: list[MemoryEntry] = field(default_factory=list)
    cold: list[MemoryEntry] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(cls, resp: dict | None) -> "RecallResult":
        if not resp or not resp.get("data"):
            return cls()
        data = resp["data"]
        return cls(
            hot=[MemoryEntry.from_dict(x) for x in data.get("hot", [])],
            cold=[MemoryEntry.from_dict(x) for x in data.get("cold", [])],
            raw=data,
        )

    def filtered_cold(
        self,
        min_score: float = 0.0,
        max_distance: float | None = None,
    ) -> list[MemoryEntry]:
        """Apply score/distance gates and return only passing cold entries."""
        out: list[MemoryEntry] = []
        for e in self.cold:
            if e.rrf_score < min_score:
                continue
            if max_distance is not None and e.vec_distance is not None and e.vec_distance > max_distance:
                continue
            out.append(e)
        return out
