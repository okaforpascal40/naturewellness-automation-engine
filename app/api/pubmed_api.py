"""PubMed API integration via NCBI E-utilities — evidence grading.

Docs: https://www.ncbi.nlm.nih.gov/books/NBK25500/

Two endpoints used:
  1. esearch.fcgi  →  count of PMIDs matching a query
  2. esummary.fcgi →  citation metadata for a list of PMIDs

NCBI rate limits: 3 req/sec without an API key, 10 req/sec with one.
Set NCBI_API_KEY in the environment to lift the limit.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ESEARCH_URL = f"{EUTILS_BASE}/esearch.fcgi"
ESUMMARY_URL = f"{EUTILS_BASE}/esummary.fcgi"

# Grade thresholds (publications matching the query).
_GRADE_A_MIN = 20
_GRADE_B_MIN = 5
_GRADE_C_MIN = 1

# Hard cap on (phytochemical, gene) pairs per pipeline run. Callers should rank
# their candidate pairs (e.g. by CTD publication_count) and slice to this many
# before calling grade_evidence — keeps total response time bounded.
MAX_PAIRS_PER_RUN = 20

# NCBI asks all clients to identify themselves.
_TOOL_NAME = "naturewellness-automation-engine"
_TOOL_EMAIL = os.getenv("NCBI_TOOL_EMAIL", "")


def _common_params() -> dict[str, str]:
    """Identification + optional API key params shared across E-utilities calls."""
    params: dict[str, str] = {"tool": _TOOL_NAME}
    if _TOOL_EMAIL:
        params["email"] = _TOOL_EMAIL
    api_key = os.getenv("NCBI_API_KEY", "")
    if api_key:
        params["api_key"] = api_key
    return params


def _grade_for_count(count: int) -> str:
    """Map a publication count to an evidence grade (A / B / C / None)."""
    if count >= _GRADE_A_MIN:
        return "A"
    if count >= _GRADE_B_MIN:
        return "B"
    if count >= _GRADE_C_MIN:
        return "C"
    return "None"


async def get_publication_count(
    phytochemical: str,
    gene: str,
    timeout: float = 30.0,
) -> int:
    """Count PubMed records matching '{phytochemical} AND {gene}'.

    Returns 0 on any network/parse error so the caller can grade gracefully.
    """
    if not phytochemical.strip() or not gene.strip():
        return 0

    params = {
        **_common_params(),
        "db": "pubmed",
        "term": f"{phytochemical} AND {gene}",
        "retmode": "json",
        "rettype": "count",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(ESEARCH_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "PubMed esearch failed [%s]: %s",
            exc.response.status_code,
            exc.response.text[:300],
        )
        return 0
    except (httpx.RequestError, ValueError) as exc:
        logger.error("PubMed esearch request error: %s", exc)
        return 0

    try:
        return int(payload["esearchresult"]["count"])
    except (KeyError, TypeError, ValueError):
        logger.warning("Unexpected PubMed esearch payload shape")
        return 0


async def _fetch_pmids(
    phytochemical: str,
    gene: str,
    retmax: int,
    timeout: float,
) -> list[str]:
    """Return up to `retmax` PMIDs for the query, sorted by relevance."""
    params = {
        **_common_params(),
        "db": "pubmed",
        "term": f"{phytochemical} AND {gene}",
        "retmode": "json",
        "retmax": str(retmax),
        "sort": "relevance",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(ESEARCH_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "PubMed esearch (idlist) failed [%s]: %s",
            exc.response.status_code,
            exc.response.text[:300],
        )
        return []
    except (httpx.RequestError, ValueError) as exc:
        logger.error("PubMed esearch (idlist) request error: %s", exc)
        return []

    try:
        return list(payload["esearchresult"]["idlist"]) or []
    except (KeyError, TypeError):
        return []


async def get_publication_details(
    pmids: list[str],
    timeout: float = 30.0,
) -> list[str]:
    """Fetch citation metadata for a list of PMIDs.

    Returns a list of formatted citation strings: 'Smith et al., 2020, J Nutr'.
    """
    if not pmids:
        return []

    params = {
        **_common_params(),
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(ESUMMARY_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "PubMed esummary failed [%s]: %s",
            exc.response.status_code,
            exc.response.text[:300],
        )
        return []
    except (httpx.RequestError, ValueError) as exc:
        logger.error("PubMed esummary request error: %s", exc)
        return []

    result: dict[str, Any] = payload.get("result", {}) if isinstance(payload, dict) else {}
    uids: list[str] = list(result.get("uids", []))

    citations: list[str] = []
    for uid in uids:
        record = result.get(uid)
        if not isinstance(record, dict):
            continue
        citations.append(_format_citation(record))
    return citations


def _format_citation(record: dict[str, Any]) -> str:
    """Build 'First-author et al., YEAR, Journal' from an esummary record."""
    authors = record.get("authors") or []
    first_author = ""
    if authors and isinstance(authors[0], dict):
        first_author = (authors[0].get("name") or "").strip()
    if first_author:
        # NCBI returns "Smith J" — keep just the surname for brevity.
        first_author = first_author.split()[0]

    pubdate: str = (record.get("pubdate") or "").strip()
    year = pubdate[:4] if pubdate[:4].isdigit() else ""

    journal: str = (record.get("source") or "").strip()
    suffix = " et al." if len(authors) > 1 else ""
    parts = [p for p in (f"{first_author}{suffix}" if first_author else "", year, journal) if p]
    return ", ".join(parts) if parts else "Unknown citation"


async def grade_evidence(
    phytochemical: str,
    gene: str,
    sample_size: int = 5,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """End-to-end: count → grade → fetch sample citations.

    Returns:
        {
          "publication_count": int,
          "evidence_grade": "A" | "B" | "C" | "None",
          "sample_citations": ["Smith et al., 2020, J Nutr", ...]
        }
    """
    count = await get_publication_count(phytochemical, gene, timeout=timeout)
    grade = _grade_for_count(count)

    sample_citations: list[str] = []
    if count > 0 and sample_size > 0:
        pmids = await _fetch_pmids(phytochemical, gene, retmax=sample_size, timeout=timeout)
        if pmids:
            sample_citations = await get_publication_details(pmids, timeout=timeout)

    return {
        "publication_count": count,
        "evidence_grade": grade,
        "sample_citations": sample_citations,
    }


get_citation_details = get_publication_details
