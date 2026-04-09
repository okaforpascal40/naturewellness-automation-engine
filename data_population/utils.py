"""Shared utilities for all data_population scripts."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

# ── Path bootstrap ────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Directory layout ──────────────────────────────────────────────────────────
DATA_DIR      = Path(__file__).parent
CHECKPOINT_DIR = DATA_DIR / "checkpoints"
REPORTS_DIR    = DATA_DIR / "reports"
LOG_DIR        = DATA_DIR / "logs"

for _d in (CHECKPOINT_DIR, REPORTS_DIR, LOG_DIR):
    _d.mkdir(exist_ok=True)


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(name: str, level: str = "INFO") -> logging.Logger:
    """Return a logger writing to both console and a date-stamped log file."""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if logger.handlers:          # avoid adding duplicate handlers on re-import
        return logger

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    fh = logging.FileHandler(LOG_DIR / f"{name}_{datetime.now():%Y%m%d}.log")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


# ── Checkpoints ───────────────────────────────────────────────────────────────

def load_checkpoint(name: str) -> dict[str, Any]:
    """Load a JSON checkpoint dict; returns blank skeleton on first run."""
    path = CHECKPOINT_DIR / f"{name}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {"processed": [], "failed": [], "last_updated": None}


def save_checkpoint(name: str, data: dict[str, Any]) -> None:
    """Persist checkpoint dict to disk."""
    data["last_updated"] = datetime.now().isoformat()
    path = CHECKPOINT_DIR / f"{name}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── Database helpers ──────────────────────────────────────────────────────────

async def batch_insert(
    client: Any,
    table: str,
    records: list[dict[str, Any]],
    batch_size: int = 100,
    dry_run: bool = False,
    logger: logging.Logger | None = None,
) -> int:
    """Insert records in batches; returns count of rows sent to the database."""
    if not records:
        return 0
    log = logger or logging.getLogger(__name__)
    total = 0

    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        if dry_run:
            log.info("[DRY RUN] Would insert %d rows into '%s'", len(batch), table)
            total += len(batch)
            continue
        try:
            await client.table(table).insert(batch).execute()
            total += len(batch)
            log.debug("Inserted %d rows into '%s' (running total: %d)", len(batch), table, total)
        except Exception as exc:
            log.error("Batch insert failed on '%s': %s", table, exc)
            raise

    return total


async def get_existing_values(
    client: Any,
    table: str,
    column: str,
) -> set[str]:
    """Return the set of all values already in *column* of *table*.

    Used to skip records that are already present without relying on
    database-level UNIQUE constraints.
    """
    response = await client.table(table).select(column).execute()
    return {str(row[column]) for row in (response.data or []) if row.get(column)}


# ── HTTP helpers ──────────────────────────────────────────────────────────────

async def safe_get(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any] | None = None,
    logger: logging.Logger | None = None,
    retries: int = 3,
    backoff_base: float = 2.0,
) -> Any:
    """GET with exponential-backoff retries; returns None on permanent failure."""
    log = logger or logging.getLogger(__name__)
    for attempt in range(retries):
        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                wait = backoff_base ** attempt
                log.warning("Rate-limited on %s — waiting %.0fs", url, wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            log.warning(
                "HTTP %d from %s (attempt %d): %s",
                exc.response.status_code, url, attempt + 1, exc.response.text[:120],
            )
            if attempt < retries - 1:
                await asyncio.sleep(backoff_base ** attempt)
        except Exception as exc:
            log.warning("Request error for %s (attempt %d): %s", url, attempt + 1, exc)
            if attempt < retries - 1:
                await asyncio.sleep(backoff_base ** attempt)
    return None


async def safe_post(
    client: httpx.AsyncClient,
    url: str,
    data: dict[str, Any] | None = None,
    logger: logging.Logger | None = None,
    retries: int = 3,
) -> Any:
    """POST with retries; returns None on permanent failure."""
    log = logger or logging.getLogger(__name__)
    for attempt in range(retries):
        try:
            resp = await client.post(url, data=data)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "json" in content_type:
                return resp.json()
            return resp.text
        except Exception as exc:
            log.warning("POST error for %s (attempt %d): %s", url, attempt + 1, exc)
            if attempt < retries - 1:
                await asyncio.sleep(2.0 ** attempt)
    return None


# ── Rate limiter ──────────────────────────────────────────────────────────────

class RateLimiter:
    """Simple token-bucket rate limiter for async code."""

    def __init__(self, calls_per_second: float = 1.0) -> None:
        self._min_interval = 1.0 / calls_per_second
        self._last_call: float = 0.0

    async def wait(self) -> None:
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_call
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_call = asyncio.get_event_loop().time()
