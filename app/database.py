from __future__ import annotations

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
