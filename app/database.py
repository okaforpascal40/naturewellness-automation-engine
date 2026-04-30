from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Any

from supabase import AsyncClient, acreate_client

from app.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def _get_client_params() -> tuple[str, str]:
    settings = get_settings()
    return settings.supabase_url, settings.supabase_key


async def get_supabase() -> AsyncClient:
    """Return an async Supabase client."""
    url, key = _get_client_params()
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_KEY must be set in environment"
        )
    return await acreate_client(url, key)


async def insert_record(table: str, data: dict[str, Any]) -> dict[str, Any]:
    client = await get_supabase()
    response = await client.table(table).insert(data).execute()
    if not response.data:
        raise ValueError(f"Insert into '{table}' returned no data")
    return response.data[0]


async def upsert_record(
    table: str, data: dict[str, Any], on_conflict: str = "id"
) -> dict[str, Any]:
    client = await get_supabase()
    response = (
        await client.table(table)
        .upsert(data, on_conflict=on_conflict)
        .execute()
    )
    if not response.data:
        raise ValueError(f"Upsert into '{table}' returned no data")
    return response.data[0]


async def fetch_records(
    table: str,
    filters: dict[str, Any] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    client = await get_supabase()
    query = client.table(table).select("*")
    for column, value in (filters or {}).items():
        query = query.eq(column, value)
    response = await query.range(offset, offset + limit - 1).execute()
    return response.data or []


async def fetch_one(
    table: str, filters: dict[str, Any]
) -> dict[str, Any] | None:
    records = await fetch_records(table, filters, limit=1)
    return records[0] if records else None


async def update_record(
    table: str, record_id: str, data: dict[str, Any]
) -> dict[str, Any]:
    client = await get_supabase()
    response = (
        await client.table(table).update(data).eq("id", record_id).execute()
    )
    if not response.data:
        raise ValueError(f"Update on '{table}' id={record_id} returned no data")
    return response.data[0]


async def count_records(
    table: str, filters: dict[str, Any] | None = None
) -> int:
    client = await get_supabase()
    query = client.table(table).select("id", count="exact")
    for column, value in (filters or {}).items():
        query = query.eq(column, value)
    response = await query.execute()
    return response.count or 0


async def get_fruits_for_phytochemicals(
    phytochemical_names: list[str],
) -> dict[str, list[str]]:
    """Return a {phytochemical_name: [fruit/veg list]} mapping.

    Lookup is case-insensitive — input names from CTD may use mixed casing
    (e.g. "Quercetin" vs "quercetin"). Phytochemicals with no row in
    phytochemical_sources are simply omitted from the returned dict.
    """
    if not phytochemical_names:
        return {}

    seen: set[str] = set()
    deduped: list[str] = []
    for name in phytochemical_names:
        s = (name or "").strip()
        key = s.lower()
        if s and key not in seen:
            seen.add(key)
            deduped.append(s)

    client = await get_supabase()
    response = (
        await client.table("phytochemical_sources")
        .select("phytochemical_name,fruit_vegetables")
        .in_("phytochemical_name", deduped)
        .execute()
    )
    by_lower: dict[str, list[str]] = {}
    for row in response.data or []:
        name = (row.get("phytochemical_name") or "").strip()
        if name:
            by_lower[name.lower()] = list(row.get("fruit_vegetables") or [])

    # Per-name case-insensitive lookup for any misses, run in parallel.
    # Doing one .ilike() call per name avoids the PostgREST .or_() comma-parsing
    # bug that breaks on chemical names containing commas (e.g.
    # "apigenin-6,8-di-C-glycopyranoside").
    misses = [n for n in deduped if n.lower() not in by_lower]
    if misses:
        async def lookup_one(name: str) -> tuple[str, list[str]] | None:
            try:
                resp = await (
                    client.table("phytochemical_sources")
                    .select("phytochemical_name,fruit_vegetables")
                    .ilike("phytochemical_name", name)
                    .limit(1)
                    .execute()
                )
            except Exception as exc:
                logger.warning("phytochemical lookup failed for %r: %s", name, exc)
                return None
            rows = resp.data or []
            if not rows:
                return None
            row_name = (rows[0].get("phytochemical_name") or "").strip()
            return (row_name, list(rows[0].get("fruit_vegetables") or []))

        for item in await asyncio.gather(*(lookup_one(n) for n in misses)):
            if item is None:
                continue
            row_name, fruits = item
            if row_name:
                by_lower[row_name.lower()] = fruits

    # Re-key the result by the caller's original input casing.
    result: dict[str, list[str]] = {}
    for original in phytochemical_names:
        s = (original or "").strip()
        if not s:
            continue
        match = by_lower.get(s.lower())
        if match is not None:
            result[s] = match
    return result
