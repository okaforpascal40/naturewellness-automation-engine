"""Gene ID conversion service using MyGene.info API.

Converts Ensembl gene IDs (ENSG…) to NCBI Entrez IDs (numeric strings)
required by the KEGG REST API.

Includes an in-process LRU-style cache so the same Ensembl ID is never
looked up twice within a server lifetime.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_MYGENE_BASE = "https://mygene.info/v3"

# Module-level caches (stable IDs — safe to cache for process lifetime)
_cache:        dict[str, Optional[str]] = {}   # ensembl_id  → entrez_id
_symbol_cache: dict[str, Optional[str]] = {}   # gene_symbol → entrez_id


def _is_ensembl_id(gene_id: str) -> bool:
    """Return True for human Ensembl gene IDs (ENSG prefix)."""
    return gene_id.upper().startswith("ENSG")


async def convert_ensembl_to_entrez(
    ensembl_ids: list[str],
    gene_symbols: list[str],
) -> dict[str, Optional[str]]:
    """Bulk-convert Ensembl IDs to Entrez IDs via MyGene.info.

    Args:
        ensembl_ids:   Parallel list of Ensembl IDs  (e.g. "ENSG00000112164").
        gene_symbols:  Parallel list of gene symbols (e.g. "GLP1R").

    Returns:
        Dict mapping gene_symbol → entrez_id string, or None if not found.
    """
    if not ensembl_ids:
        return {}

    results: dict[str, Optional[str]] = {}

    # ── Resolve from cache first ──────────────────────────────────────────────
    uncached_ensembl: list[str] = []
    uncached_symbols: list[str] = []
    for ensembl_id, sym in zip(ensembl_ids, gene_symbols):
        if ensembl_id in _cache:
            results[sym] = _cache[ensembl_id]
        else:
            uncached_ensembl.append(ensembl_id)
            uncached_symbols.append(sym)

    if not uncached_ensembl:
        logger.debug("mygene: all %d IDs resolved from cache", len(results))
        return results

    logger.debug(
        "mygene: converting %d Ensembl ID(s) via API (cached: %d)",
        len(uncached_ensembl),
        len(results),
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        # ── Phase 1: batch Ensembl lookup ─────────────────────────────────────
        try:
            resp = await client.post(
                f"{_MYGENE_BASE}/query",
                data={
                    "q":       ",".join(uncached_ensembl),
                    "scopes":  "ensembl.gene",
                    "fields":  "entrezgene,symbol",
                    "species": "human",
                },
            )
            resp.raise_for_status()
            items = resp.json()
            if isinstance(items, list):
                for i, item in enumerate(items):
                    sym = uncached_symbols[i] if i < len(uncached_symbols) else None
                    eid = uncached_ensembl[i] if i < len(uncached_ensembl) else None
                    if not sym:
                        continue
                    entrez = item.get("entrezgene")
                    entrez_str = str(int(entrez)) if entrez is not None else None
                    results[sym] = entrez_str
                    if eid:
                        _cache[eid] = entrez_str
                    if entrez_str:
                        logger.debug("mygene: %s (%s) → Entrez %s", sym, eid, entrez_str)
                    else:
                        logger.debug("mygene: no Entrez ID for %s (%s)", sym, eid)
        except Exception as exc:
            logger.warning("mygene Ensembl batch lookup failed: %s", exc)
            # Cache None for all so we don't retry on subsequent calls
            for eid, sym in zip(uncached_ensembl, uncached_symbols):
                _cache.setdefault(eid, None)
                results.setdefault(sym, None)
            return results

        # ── Phase 2: symbol fallback for any still-missing entries ────────────
        still_missing_syms   = [s for s in uncached_symbols if not results.get(s)]
        still_missing_ensembl = [
            uncached_ensembl[uncached_symbols.index(s)]
            for s in still_missing_syms
        ]
        if still_missing_syms:
            logger.debug(
                "mygene: %d gene(s) unresolved via Ensembl, trying symbol fallback: %s",
                len(still_missing_syms),
                ", ".join(still_missing_syms[:10]),
            )
            try:
                resp2 = await client.post(
                    f"{_MYGENE_BASE}/query",
                    data={
                        "q":       ",".join(still_missing_syms),
                        "scopes":  "symbol",
                        "fields":  "entrezgene",
                        "species": "human",
                    },
                )
                resp2.raise_for_status()
                items2 = resp2.json()
                if isinstance(items2, list):
                    for i, item in enumerate(items2):
                        sym = still_missing_syms[i] if i < len(still_missing_syms) else None
                        eid = still_missing_ensembl[i] if i < len(still_missing_ensembl) else None
                        if not sym:
                            continue
                        entrez = item.get("entrezgene")
                        entrez_str = str(int(entrez)) if entrez is not None else None
                        results[sym] = entrez_str
                        if eid:
                            _cache[eid] = entrez_str
                        if entrez_str:
                            logger.debug(
                                "mygene: symbol fallback: %s (%s) → Entrez %s",
                                sym, eid, entrez_str,
                            )
            except Exception as exc:
                logger.warning("mygene symbol fallback failed: %s", exc)
                for sym, eid in zip(still_missing_syms, still_missing_ensembl):
                    results.setdefault(sym, None)
                    if eid:
                        _cache.setdefault(eid, None)

    converted = sum(1 for v in results.values() if v)
    logger.info(
        "mygene: converted %d/%d gene(s) to Entrez IDs",
        converted,
        len(ensembl_ids),
    )
    return results


async def _lookup_by_symbol(gene_symbol: str) -> Optional[str]:
    """Return the Entrez ID for a gene symbol using a direct MyGene.info GET query."""
    if gene_symbol in _symbol_cache:
        cached = _symbol_cache[gene_symbol]
        logger.debug("mygene symbol cache hit: %s → %s", gene_symbol, cached)
        return cached

    logger.debug("mygene: symbol lookup for %s", gene_symbol)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{_MYGENE_BASE}/query",
                params={
                    "q":       gene_symbol,
                    "scopes":  "symbol",
                    "fields":  "entrezgene",
                    "species": "human",
                    "size":    1,
                },
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
            if hits and "entrezgene" in hits[0]:
                entrez = str(int(hits[0]["entrezgene"]))
                _symbol_cache[gene_symbol] = entrez
                logger.debug("mygene symbol lookup: %s → Entrez %s", gene_symbol, entrez)
                return entrez
    except Exception as exc:
        logger.warning("mygene symbol lookup failed for %s: %s", gene_symbol, exc)

    _symbol_cache[gene_symbol] = None
    logger.debug("mygene: no Entrez ID found for symbol %s", gene_symbol)
    return None


async def get_entrez_id(gene_symbol: str, gene_id: str | None) -> Optional[str]:
    """Return the Entrez ID for a single gene.

    Resolution priority:
    1. *gene_id* is already a numeric Entrez ID → return as-is.
    2. *gene_id* is an Ensembl ID (ENSG…) → convert via MyGene.info batch API.
    3. *gene_id* is None / empty → fall back to symbol-based lookup.
    """
    if not gene_id:
        # No gene_id provided — resolve by symbol
        return await _lookup_by_symbol(gene_symbol)

    if not _is_ensembl_id(gene_id):
        # Already a numeric Entrez ID
        return gene_id

    # Ensembl ID path — check cache first
    if gene_id in _cache:
        cached = _cache[gene_id]
        logger.debug("mygene cache hit: %s → %s", gene_id, cached)
        if cached is None:
            # Ensembl lookup failed previously — try symbol fallback
            return await _lookup_by_symbol(gene_symbol)
        return cached

    conversion = await convert_ensembl_to_entrez([gene_id], [gene_symbol])
    entrez = conversion.get(gene_symbol)
    if not entrez:
        logger.debug(
            "mygene: Ensembl lookup for %s returned nothing — trying symbol fallback",
            gene_symbol,
        )
        return await _lookup_by_symbol(gene_symbol)
    return entrez
