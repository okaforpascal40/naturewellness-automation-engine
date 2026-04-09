"""Supabase curated-database matcher.

Replaces all external compound/pathway/food API calls (ChEMBL, KEGG, Reactome,
FooDB, USDA, HMDB) with queries against the project's own Supabase tables.

Gene lookup is keyed on gene_symbol because that is what Open Targets returns.
All three public functions are safe to call with an empty input list — they
return [] immediately without hitting the database.

Confirmed parent-table column names:
  genes      → id (uuid), gene_symbol (text)
  compounds  → id (uuid), compound_name (text)
  foods      → id (uuid), name (text)
  pathways   → id (uuid), pathway_id (text), pathway_name (text)
"""
from __future__ import annotations

import logging
from typing import Any

from app.database import get_supabase
from app.models import CompoundGeneInteraction, FoodCompoundMapping, GenePathwayMapping

logger = logging.getLogger(__name__)


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _resolve_gene_uuids(gene_symbols: list[str]) -> dict[str, str]:
    """Return {gene_symbol: supabase_uuid} for each symbol found in the genes table.

    [ASSUMPTION] genes table has columns: id (uuid), gene_symbol (text)
    """
    client = await get_supabase()
    response = (
        await client.table("genes")
        .select("id, gene_symbol")
        .in_("gene_symbol", gene_symbols)
        .execute()
    )
    return {row["gene_symbol"]: row["id"] for row in (response.data or [])}


async def _resolve_compound_uuids(compound_names: list[str]) -> dict[str, str]:
    """Return {compound_name: supabase_uuid} for each name found in the compounds table."""
    client = await get_supabase()
    response = (
        await client.table("compounds")
        .select("id, compound_name")
        .in_("compound_name", compound_names)
        .execute()
    )
    return {row["compound_name"]: row["id"] for row in (response.data or [])}


# ── Public API ────────────────────────────────────────────────────────────────

async def match_genes_to_compounds(
    gene_symbols: list[str],
) -> list[CompoundGeneInteraction]:
    """Return curated compound–gene interactions for the given gene symbols.

    Queries compound_gene_interactions joined with compounds.
    Returns an empty list if none of the symbols exist in the database.
    """
    if not gene_symbols:
        return []

    symbol_to_uuid = await _resolve_gene_uuids(gene_symbols)
    if not symbol_to_uuid:
        logger.debug(
            "supabase_matcher: none of %s found in genes table", gene_symbols
        )
        return []

    gene_uuids = list(symbol_to_uuid.values())
    uuid_to_symbol: dict[str, str] = {v: k for k, v in symbol_to_uuid.items()}

    client = await get_supabase()
    response = (
        await client.table("compound_gene_interactions")
        .select(
            "id, gene_id, interaction_type, bioactivity_score, evidence_type, source,"
            " compounds(id, compound_name)"
        )
        .in_("gene_id", gene_uuids)
        .execute()
    )

    interactions: list[CompoundGeneInteraction] = []
    for row in (response.data or []):
        compound_rec: dict[str, Any] = row.get("compounds") or {}
        compound_name: str = compound_rec.get("compound_name", "")
        compound_uuid: str = compound_rec.get("id", "")
        gene_uuid: str = row.get("gene_id", "")
        gene_symbol: str = uuid_to_symbol.get(gene_uuid, "")

        if not compound_name or not gene_symbol:
            logger.warning(
                "supabase_matcher: skipping incomplete compound_gene_interactions row: %s",
                row,
            )
            continue

        try:
            interactions.append(
                CompoundGeneInteraction(
                    compound_id=compound_uuid,
                    compound_name=compound_name,
                    gene_id=gene_uuid,
                    gene_symbol=gene_symbol,
                    activity_type=row.get("interaction_type"),
                    activity_value=_safe_float(row.get("bioactivity_score")),
                    activity_units=None,
                    source=row.get("source") or "supabase",
                )
            )
        except Exception:
            logger.warning(
                "supabase_matcher: skipping malformed compound_gene_interactions row: %s",
                row,
            )

    logger.info(
        "supabase_matcher: %d compound interaction(s) for %d gene symbol(s)",
        len(interactions),
        len(gene_symbols),
    )
    return interactions


async def get_foods_for_compounds(
    compound_names: list[str],
) -> list[FoodCompoundMapping]:
    """Return curated food–compound mappings for the given compound names.

    Queries food_compound_associations joined with foods and compounds.
    Returns an empty list if none of the compounds exist in the database.
    """
    if not compound_names:
        return []

    name_to_uuid = await _resolve_compound_uuids(compound_names)
    if not name_to_uuid:
        logger.debug(
            "supabase_matcher: none of %s found in compounds table", compound_names
        )
        return []

    compound_uuids = list(name_to_uuid.values())
    uuid_to_name: dict[str, str] = {v: k for k, v in name_to_uuid.items()}

    client = await get_supabase()
    # [ASSUMPTION] foods.name holds the human-readable food name
    response = (
        await client.table("food_compound_associations")
        .select(
            "id, food_id, compound_id,"
            " concentration_mg_per_100g, concentration_category, source,"
            " foods(id, name)"
        )
        .in_("compound_id", compound_uuids)
        .execute()
    )

    mappings: list[FoodCompoundMapping] = []
    for row in (response.data or []):
        food_rec: dict[str, Any] = row.get("foods") or {}
        food_name: str = food_rec.get("name", "")
        food_uuid: str = food_rec.get("id", "") or row.get("food_id", "")
        compound_uuid: str = row.get("compound_id", "")
        compound_name: str = uuid_to_name.get(compound_uuid, "")

        if not food_name or not compound_name:
            logger.warning(
                "supabase_matcher: skipping incomplete food_compound_associations row: %s",
                row,
            )
            continue

        try:
            mappings.append(
                FoodCompoundMapping(
                    food_id=food_uuid,
                    food_name=food_name,
                    compound_name=compound_name,
                    compound_amount=_safe_float(row.get("concentration_mg_per_100g")),
                    compound_unit="mg/100g" if row.get("concentration_mg_per_100g") is not None else None,
                    source=row.get("source") or "supabase",
                )
            )
        except Exception:
            logger.warning(
                "supabase_matcher: skipping malformed food_compound_associations row: %s",
                row,
            )

    logger.info(
        "supabase_matcher: %d food mapping(s) for %d compound name(s)",
        len(mappings),
        len(compound_names),
    )
    return mappings


async def get_pathways_for_genes(
    gene_symbols: list[str],
) -> list[GenePathwayMapping]:
    """Return curated gene–pathway mappings for the given gene symbols.

    Queries gene_pathway_associations joined with genes and pathways.
    Returns an empty list if none of the symbols exist in the database.
    """
    if not gene_symbols:
        return []

    symbol_to_uuid = await _resolve_gene_uuids(gene_symbols)
    if not symbol_to_uuid:
        logger.debug(
            "supabase_matcher: none of %s found in genes table (pathways lookup)",
            gene_symbols,
        )
        return []

    gene_uuids = list(symbol_to_uuid.values())
    uuid_to_symbol: dict[str, str] = {v: k for k, v in symbol_to_uuid.items()}

    client = await get_supabase()
    response = (
        await client.table("gene_pathway_associations")
        .select(
            "id, gene_id, confidence_score,"
            " pathways(id, pathway_id, pathway_name)"
        )
        .in_("gene_id", gene_uuids)
        .execute()
    )

    mappings: list[GenePathwayMapping] = []
    for row in (response.data or []):
        pathway_rec: dict[str, Any] = row.get("pathways") or {}
        pathway_name: str = pathway_rec.get("pathway_name", "")
        # Use the domain pathway_id (text) when available; fall back to the row UUID
        pathway_uuid: str = pathway_rec.get("pathway_id") or pathway_rec.get("id", "")
        gene_uuid: str = row.get("gene_id", "")
        gene_symbol: str = uuid_to_symbol.get(gene_uuid, "")

        if not pathway_name or not gene_symbol:
            logger.warning(
                "supabase_matcher: skipping incomplete gene_pathway_associations row: %s",
                row,
            )
            continue

        try:
            mappings.append(
                GenePathwayMapping(
                    gene_id=gene_uuid,
                    gene_symbol=gene_symbol,
                    pathway_id=pathway_uuid,
                    pathway_name=pathway_name,
                    species="Homo sapiens",
                    source="supabase",
                )
            )
        except Exception:
            logger.warning(
                "supabase_matcher: skipping malformed gene_pathway_associations row: %s",
                row,
            )

    logger.info(
        "supabase_matcher: %d pathway mapping(s) for %d gene symbol(s)",
        len(mappings),
        len(gene_symbols),
    )
    return mappings


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
