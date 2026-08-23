"""Literature-first compound lookup for diseases with no usable gene data.

When Open Targets yields nothing even after lowering the score floor and trying
synonyms, we ask PubMed directly: which phytochemicals have actually been
studied against this condition? Each compound is confirmed by a real search hit
and carries the PMIDs behind it, so the References tab stays populated and a
reader can check the claim — which is the whole point of the evidence UI.

This is weaker evidence than the gene-pathway chain: a PubMed hit means the
pairing has been studied, not that it works. Callers must label it as such.

E-utilities: https://eutils.ncbi.nlm.nih.gov/entrez/eutils/
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"

# Compounds worth testing against a disease: those we can actually name a food
# source for. Querying all 1,689 CTD compounds would be thousands of requests;
# these are the well-studied dietary bioactives that also appear in
# phytochemical_sources, so a hit can become a recommendation.
_CANDIDATE_COMPOUNDS = (
    "Quercetin",
    "Curcumin",
    "Resveratrol",
    "Epigallocatechin gallate",
    "Berberine",
    "Allicin",
    "Sulforaphane",
    "Genistein",
    "Luteolin",
    "Apigenin",
    "Kaempferol",
    "Naringenin",
    "Hesperidin",
    "Lycopene",
    "Silymarin",
    "Capsaicin",
    "Gingerol",
    "Thymoquinone",
    "Ellagic acid",
    "Caffeic acid",
    "Chlorogenic acid",
    "Anthocyanins",
    "Beta-carotene",
    "Piperine",
    "Eugenol",
)

# NCBI asks for <=3 requests/second without a key, 10 with one.
_CONCURRENCY_NO_KEY = 2
_CONCURRENCY_WITH_KEY = 6
_PMIDS_PER_COMPOUND = 5
_MIN_HITS = 2  # below this the pairing is too thinly studied to surface


def _build_query(disease_name: str, compound: str) -> str:
    """Restrict to titles/abstracts so incidental mentions do not count."""
    disease = disease_name.replace('"', "").strip()
    chem = compound.replace('"', "").strip()
    return f'("{disease}"[Title/Abstract]) AND ("{chem}"[Title/Abstract])'


async def _search(
    client: httpx.AsyncClient,
    query: str,
    api_key: str,
) -> tuple[int, list[str]]:
    params: dict[str, Any] = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": _PMIDS_PER_COMPOUND,
        "sort": "relevance",
    }
    if api_key:
        params["api_key"] = api_key

    response = await client.get(_ESEARCH, params=params)
    response.raise_for_status()
    result = (response.json() or {}).get("esearchresult") or {}
    try:
        count = int(result.get("count", 0))
    except (TypeError, ValueError):
        count = 0
    pmids = [str(p) for p in (result.get("idlist") or []) if str(p).strip()]
    return count, pmids


async def get_phytochemicals_for_disease(
    disease_name: str,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Phytochemicals with published studies against this disease.

    Returns rows in the shape `ctd_api.get_chemicals_for_genes` emits, so
    chain_builder can consume them unchanged. `gene_symbol` is empty: this
    route deliberately has no gene claim to make.

    Never raises — a fallback that breaks the request is worse than no fallback.
    """
    name = (disease_name or "").strip()
    if not name:
        return []

    api_key = get_settings().ncbi_api_key
    concurrency = _CONCURRENCY_WITH_KEY if api_key else _CONCURRENCY_NO_KEY
    semaphore = asyncio.Semaphore(concurrency)

    async def one(compound: str) -> dict[str, Any] | None:
        async with semaphore:
            try:
                count, pmids = await _search(client, _build_query(name, compound), api_key)
            except Exception as exc:  # noqa: BLE001 - skip this compound only
                logger.warning("PubMed lookup failed for %r + %r: %s", name, compound, exc)
                return None
        if count < _MIN_HITS or not pmids:
            return None
        return {
            "chemical_name": compound,
            "gene_symbol": "",
            "gene_id": "",
            "interaction_type": "studied in",
            "publication_count": count,
            "pmids": pmids,
            "source": "PubMed",
        }

    async with httpx.AsyncClient(timeout=30.0) as client:
        results = await asyncio.gather(*(one(c) for c in _CANDIDATE_COMPOUNDS))

    rows = [r for r in results if r]
    rows.sort(key=lambda r: r["publication_count"], reverse=True)
    logger.info(
        "PubMed fallback: %d/%d compound(s) with >=%d studies for %r",
        len(rows),
        len(_CANDIDATE_COMPOUNDS),
        _MIN_HITS,
        name,
    )
    return rows[:limit]
