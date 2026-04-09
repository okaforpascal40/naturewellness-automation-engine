"""KEGG (Kyoto Encyclopedia of Genes and Genomes) REST API integration.

Docs: https://www.kegg.jp/kegg/rest/keggapi.html

KEGG provides both pathway and compound data in one database, replacing the
need for separate Reactome (pathways) and ChEMBL (compounds) calls.

All KEGG REST responses are plain text — TSV for link/find endpoints and a
custom flat-file format for get endpoints.  There is no JSON.

Compound ID prefix convention:
  C##### — metabolite / natural dietary compound  → KEEP
  D##### — approved drug / pharmaceutical          → SKIP
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from app.api.mygene_converter import get_entrez_id
from app.models import CompoundGeneInteraction, GenePathwayMapping

logger = logging.getLogger(__name__)

_BASE_URL = "https://rest.kegg.jp"
# Max entries KEGG allows in a single /get request
_GET_BATCH_SIZE = 10
# Pause between sequential HTTP calls to respect KEGG's rate limits
_RATE_LIMIT_DELAY = 0.5


# ── Low-level helpers ─────────────────────────────────────────────────────────

async def _get_text(client: httpx.AsyncClient, path: str) -> str:
    """GET a KEGG REST endpoint and return the response body as text."""
    url = f"{_BASE_URL}{path}"
    try:
        response = await client.get(url)
        response.raise_for_status()
        return response.text
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return ""
        logger.error("KEGG request failed for %s [%s]: %s", path, exc.response.status_code, exc.response.text[:200])
        raise
    except httpx.RequestError as exc:
        logger.warning("KEGG request error for %s: %s", path, exc)
        raise


def _parse_tsv_column(text: str, column: int) -> list[str]:
    """Extract *column* (0-indexed) from a tab-separated KEGG link/find response."""
    results: list[str] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) > column and parts[column].strip():
            results.append(parts[column].strip())
    return results


def _parse_flat_file_field(text: str, field: str) -> str:
    """Return the first value for *field* in a KEGG flat-file record.

    KEGG flat files look like:
        NAME        Quercetin
                    3,3',4',5,7-Pentahydroxyflavone
    The first token after the field name (on the same line) is the primary
    value; continuation lines are indented.
    """
    pattern = re.compile(rf"^{re.escape(field)}\s+(.+)$", re.MULTILINE)
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def _split_compound_records(text: str) -> list[str]:
    """Split a batched /get response into individual records.

    KEGG separates records with a line containing only "///".
    """
    return [r.strip() for r in text.split("///") if r.strip()]


# ── Batch pathway name resolution ────────────────────────────────────────────

async def _fetch_pathway_names(
    client: httpx.AsyncClient,
    pathway_ids: list[str],
) -> dict[str, str]:
    """Return {pathway_id: pathway_name} for up to *_GET_BATCH_SIZE* IDs per request."""
    if not pathway_ids:
        return {}

    id_to_name: dict[str, str] = {}

    for i in range(0, len(pathway_ids), _GET_BATCH_SIZE):
        batch = pathway_ids[i : i + _GET_BATCH_SIZE]
        joined = "+".join(batch)
        if i > 0:
            await asyncio.sleep(_RATE_LIMIT_DELAY)
        try:
            text = await _get_text(client, f"/get/{joined}")
        except Exception:
            continue

        for record in _split_compound_records(text):
            # Pathway entry header: "ENTRY       hsa04110   Pathway"
            entry_match = re.search(r"^ENTRY\s+(hsa\d+)", record, re.MULTILINE)
            if not entry_match:
                continue
            pid = f"path:{entry_match.group(1)}"
            name = _parse_flat_file_field(record, "NAME")
            # Strip " - Homo sapiens (human)" suffix if present
            name = re.sub(r"\s*-\s*Homo sapiens.*$", "", name).strip()
            if name:
                id_to_name[pid] = name

    return id_to_name


# ── Compound name / detail resolution ────────────────────────────────────────

async def _fetch_compound_names(
    client: httpx.AsyncClient,
    compound_ids: list[str],
) -> dict[str, str]:
    """Return {compound_id: primary_name} for a list of KEGG compound IDs.

    Only fetches C-prefix (metabolite) IDs; D-prefix drug IDs are skipped.
    """
    metabolite_ids = [cid for cid in compound_ids if _is_metabolite(cid)]
    if not metabolite_ids:
        return {}

    id_to_name: dict[str, str] = {}

    for i in range(0, len(metabolite_ids), _GET_BATCH_SIZE):
        batch = metabolite_ids[i : i + _GET_BATCH_SIZE]
        joined = "+".join(batch)
        if i > 0:
            await asyncio.sleep(_RATE_LIMIT_DELAY)
        try:
            text = await _get_text(client, f"/get/{joined}")
        except Exception:
            continue

        for record in _split_compound_records(text):
            entry_match = re.search(r"^ENTRY\s+(C\d+)", record, re.MULTILINE)
            if not entry_match:
                continue
            cid = f"cpd:{entry_match.group(1)}"
            name = _parse_flat_file_field(record, "NAME")
            # NAME may be semicolon-separated synonyms; take the first
            name = name.split(";")[0].strip()
            if name:
                id_to_name[cid] = name

    return id_to_name


def _is_metabolite(compound_id: str) -> bool:
    """Return True if this is a natural metabolite (C-prefix), False for drugs (D-prefix)."""
    # compound_id is "cpd:C00509" or "cpd:D00001"
    bare = compound_id.upper().replace("CPD:", "").strip()
    return bare.startswith("C")


# ── Public API ────────────────────────────────────────────────────────────────

async def get_pathways_for_gene(
    gene_symbol: str,
    gene_id: str | None,
) -> list[GenePathwayMapping]:
    """Fetch KEGG pathways for a human gene.

    Accepts either an Ensembl ID (ENSG…) or a numeric Entrez ID.
    Ensembl IDs are automatically converted via MyGene.info before
    constructing the KEGG hsa:{entrez_id} identifier.
    """
    entrez_id = await get_entrez_id(gene_symbol, gene_id)
    if not entrez_id:
        logger.debug(
            "KEGG pathways: skipping %s — no Entrez ID resolved from '%s'",
            gene_symbol, gene_id,
        )
        return []

    kegg_gene_id = f"hsa:{entrez_id}"
    logger.debug("KEGG pathways: querying %s for %s (gene_id=%s)", kegg_gene_id, gene_symbol, gene_id)

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            link_text = await _get_text(client, f"/link/pathway/{kegg_gene_id}")
        except Exception as exc:
            logger.warning("KEGG pathway link failed for %s (%s): %s", gene_symbol, kegg_gene_id, exc)
            return []

        if not link_text.strip():
            logger.debug("KEGG: no pathways for gene %s (%s)", gene_symbol, kegg_gene_id)
            return []

        # Each line: "hsa:7157\tpath:hsa04110"
        raw_pathway_ids = _parse_tsv_column(link_text, column=1)
        # Keep only human-specific pathway entries
        pathway_ids = [p for p in raw_pathway_ids if p.startswith("path:hsa")]

        if not pathway_ids:
            return []

        await asyncio.sleep(_RATE_LIMIT_DELAY)
        id_to_name = await _fetch_pathway_names(client, pathway_ids)

    mappings: list[GenePathwayMapping] = []
    for pid in pathway_ids:
        name = id_to_name.get(pid, "")
        if not name:
            continue
        try:
            mappings.append(
                GenePathwayMapping(
                    gene_id=entrez_id,
                    gene_symbol=gene_symbol,
                    pathway_id=pid,
                    pathway_name=name,
                    species="Homo sapiens",
                    source="kegg",
                )
            )
        except Exception as exc:
            logger.warning(
                "Skipping malformed KEGG pathway record for %s (%s): %s",
                gene_symbol, pid, exc,
            )

    logger.info("KEGG: %d pathway(s) for gene %s", len(mappings), gene_symbol)
    return mappings


async def get_compounds_for_gene(
    gene_symbol: str,
    gene_id: str | None,
    limit: int = 20,
) -> list[CompoundGeneInteraction]:
    """Fetch natural metabolites linked to a human gene via KEGG.

    Accepts either an Ensembl ID (ENSG…) or a numeric Entrez ID.
    Ensembl IDs are automatically converted via MyGene.info.
    Filters out D-prefix drug entries, keeping only C-prefix metabolites.
    """
    entrez_id = await get_entrez_id(gene_symbol, gene_id)
    if not entrez_id:
        logger.debug(
            "KEGG compounds: skipping %s — no Entrez ID resolved from '%s'",
            gene_symbol, gene_id,
        )
        return []

    kegg_gene_id = f"hsa:{entrez_id}"
    logger.debug("KEGG compounds: querying %s for %s (gene_id=%s)", kegg_gene_id, gene_symbol, gene_id)

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            link_text = await _get_text(client, f"/link/compound/{kegg_gene_id}")
        except Exception as exc:
            logger.warning("KEGG compound link failed for %s (%s): %s", gene_symbol, kegg_gene_id, exc)
            return []

        if not link_text.strip():
            logger.debug("KEGG: no compounds for gene %s (%s)", gene_symbol, kegg_gene_id)
            return []

        # Each line: "hsa:7157\tcpd:C00509"
        all_compound_ids = _parse_tsv_column(link_text, column=1)

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_ids: list[str] = []
        for cid in all_compound_ids:
            if cid not in seen:
                seen.add(cid)
                unique_ids.append(cid)

        metabolite_ids = [cid for cid in unique_ids if _is_metabolite(cid)]
        drug_count = len(unique_ids) - len(metabolite_ids)
        if drug_count:
            logger.debug(
                "KEGG: skipped %d D-prefix drug ID(s) for gene %s",
                drug_count,
                gene_symbol,
            )

        if not metabolite_ids:
            return []

        await asyncio.sleep(_RATE_LIMIT_DELAY)
        id_to_name = await _fetch_compound_names(client, metabolite_ids[:limit * 2])

    interactions: list[CompoundGeneInteraction] = []
    for cid in metabolite_ids:
        if len(interactions) >= limit:
            break
        name = id_to_name.get(cid)
        if not name:
            continue
        try:
            interactions.append(
                CompoundGeneInteraction(
                    compound_id=cid,
                    compound_name=name,
                    gene_id=entrez_id,
                    gene_symbol=gene_symbol,
                    activity_type=None,
                    activity_value=None,
                    activity_units=None,
                    source="kegg",
                )
            )
        except Exception as exc:
            logger.warning(
                "Skipping malformed KEGG compound record: %s / %s: %s", cid, name, exc
            )

    logger.info(
        "KEGG: %d natural metabolite(s) for gene %s (from %d total, %d metabolites)",
        len(interactions),
        gene_symbol,
        len(unique_ids),
        len(metabolite_ids),
    )
    return interactions


async def search_foods_by_compound_kegg(compound_name: str) -> list[str]:
    """Search KEGG for a compound by name and return food source names.

    Uses /find/compound to locate the compound, then parses the SOURCE section
    of the flat-file detail record for dietary food origins.

    Returns a list of food name strings (not FoodCompoundMapping — callers that
    need full mappings should construct them from the returned names).
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            find_text = await _get_text(client, f"/find/compound/{compound_name}")
        except Exception as exc:
            logger.warning("KEGG compound find failed for '%s': %s", compound_name, exc)
            return []

        if not find_text.strip():
            logger.debug("KEGG find: no results for '%s'", compound_name)
            return []

        # Each line: "cpd:C00509\tQuercetin; 3,3',4',5,7-Pentahydroxyflavone"
        compound_ids = _parse_tsv_column(find_text, column=0)
        metabolite_ids = [cid for cid in compound_ids if _is_metabolite(cid)]
        if not metabolite_ids:
            return []

        first_id = metabolite_ids[0]
        await asyncio.sleep(_RATE_LIMIT_DELAY)

        try:
            detail_text = await _get_text(client, f"/get/{first_id}")
        except Exception as exc:
            logger.warning("KEGG compound detail failed for %s: %s", first_id, exc)
            return []

    # Parse SOURCE section — lists food and biological origins
    food_names: list[str] = []
    in_source = False
    for line in detail_text.splitlines():
        if line.startswith("SOURCE"):
            in_source = True
            # Value may start on the same line after field name
            value = re.sub(r"^SOURCE\s+", "", line).strip()
            if value:
                food_names.append(value)
        elif in_source:
            if line and line[0] == " ":
                # Continuation line
                value = line.strip()
                if value:
                    food_names.append(value)
            else:
                # New field — SOURCE section ended
                break

    return food_names
