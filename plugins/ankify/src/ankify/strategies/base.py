"""Shared helpers for deterministic Ankify strategy resolution."""

from __future__ import annotations

from collections.abc import Iterable


def stable_tags(*groups: Iterable[str]) -> tuple[str, ...]:
    """Return trimmed, de-duplicated tags while preserving declaration order."""

    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for raw in group:
            tag = raw.strip()
            if tag and tag not in seen:
                result.append(tag)
                seen.add(tag)
    return tuple(result)


__all__ = ["stable_tags"]
