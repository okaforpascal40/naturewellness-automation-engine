"""Shared plant → phytochemical lookup and offline evidence grading.

Used by both reverse pipelines (CamScan health-benefits and plant search), so
the matching rules and grade thresholds cannot drift apart. Grade thresholds
come straight from chain_builder, which grades the same CTD PMID counts in the
forward disease → food direction.
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.chain_builder import _GRADE_A_MIN, _GRADE_B_MIN, _GRADE_C_MIN, _GRADE_RANK
from app.database import get_supabase

logger = logging.getLogger(__name__)

GRADE_RANK = _GRADE_RANK

# phytochemical_sources changes only when the seed scripts run, so a short
# in-process cache spares a full-table read on every keystroke of a search box.
_SOURCES_TTL_SECONDS = 600.0
# No lock: a concurrent miss costs one duplicate read, which is harmless.
_sources_cache: tuple[float, list[dict[str, Any]]] | None = None


@dataclass(frozen=True)
class PlantMatch:
    """Phytochemicals found for a plant, plus the source-table spelling matched."""

    phytochemicals: list[str] = field(default_factory=list)
    matched_name: str | None = None


def grade_from_pmid_count(count: int) -> str:
    """Offline evidence grade for a unique-PMID count (chain_builder thresholds)."""
    if count >= _GRADE_A_MIN:
        return "A"
    if count >= _GRADE_B_MIN:
        return "B"
    if count >= _GRADE_C_MIN:
        return "C"
    return "None"


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def match_plant(rows: list[dict[str, Any]], plant_name: str, *, exact_first: bool = False) -> PlantMatch:
    """Phytochemicals whose fruit_vegetables list contains this plant.

    Matching is case-insensitive and word-level: a source food matches when it
    equals the plant, or one name's word set is a subset of the other's (so
    "Onion" matches "Red Onion" but "Pea" does not match "Peach").

    With ``exact_first``, the result always describes ONE plant: a whole-name
    match wins outright, and the word-level rule is only a fallback when it
    resolves to a single distinct name ("red onion" → "Onion"). An ambiguous
    word ("Red" → Red Cabbage, Red tea, …) matches nothing rather than pooling
    several plants' compounds under one label.
    """
    plant_l = plant_name.strip().lower()
    if not plant_l:
        return PlantMatch()

    if exact_first:
        exact = _collect(rows, lambda fvl: fvl == plant_l)
        if exact.phytochemicals:
            return exact

    plant_tokens = _tokens(plant_l)

    def word_match(fvl: str) -> bool:
        fv_tokens = _tokens(fvl)
        return (
            fvl == plant_l
            or (bool(fv_tokens) and fv_tokens <= plant_tokens)
            or (bool(plant_tokens) and plant_tokens <= fv_tokens)
        )

    if exact_first:
        candidates = {
            fv_s.lower()
            for row in rows
            for fv in row.get("fruit_vegetables") or []
            if (fv_s := str(fv).strip()) and word_match(fv_s.lower())
        }
        if len(candidates) != 1:
            return PlantMatch()
        only = candidates.pop()
        return _collect(rows, lambda fvl: fvl == only)

    return _collect(rows, word_match)


def _collect(rows: list[dict[str, Any]], matches: Callable[[str], bool]) -> PlantMatch:
    phytochemicals: list[str] = []
    seen: set[str] = set()
    spellings: Counter[str] = Counter()

    for row in rows:
        name = (row.get("phytochemical_name") or "").strip()
        if not name or name.lower() in seen:
            continue
        for fv in row.get("fruit_vegetables") or []:
            fv_s = str(fv).strip()
            if fv_s and matches(fv_s.lower()):
                phytochemicals.append(name)
                seen.add(name.lower())
                spellings[fv_s] += 1
                break

    # The most common spelling among matched rows is the display name.
    matched_name = spellings.most_common(1)[0][0] if spellings else None
    return PlantMatch(phytochemicals=phytochemicals, matched_name=matched_name)


async def fetch_phytochemical_sources() -> list[dict[str, Any]]:
    """Every phytochemical_sources row (name + foods), uncached."""
    client = await get_supabase()
    resp = (
        await client.table("phytochemical_sources")
        .select("phytochemical_name,fruit_vegetables")
        .execute()
    )
    return list(resp.data or [])


async def cached_phytochemical_sources() -> list[dict[str, Any]]:
    """phytochemical_sources rows, served from a TTL cache when fresh."""
    global _sources_cache
    now = time.monotonic()
    if _sources_cache and now - _sources_cache[0] < _SOURCES_TTL_SECONDS:
        return _sources_cache[1]
    rows = await fetch_phytochemical_sources()
    _sources_cache = (now, rows)
    return rows


def clear_sources_cache() -> None:
    global _sources_cache
    _sources_cache = None


def plant_names(rows: list[dict[str, Any]]) -> list[str]:
    """Distinct plant names across all rows (first-seen spelling), sorted."""
    names: dict[str, str] = {}
    for row in rows:
        for fv in row.get("fruit_vegetables") or []:
            fv_s = str(fv).strip()
            if fv_s:
                names.setdefault(fv_s.lower(), fv_s)
    return sorted(names.values(), key=str.lower)
