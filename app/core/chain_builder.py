"""Disease → Gene → Pathway → Compound → Food pipeline.

Hybrid architecture:
  - Genes:              Open Targets API (automated, live)
  - Pathways/Compounds/Foods: Supabase curated database (supabase_matcher)
  - Evidence scoring:   Automated

All external API dependencies (ChEMBL, KEGG, Reactome, FooDB, USDA, HMDB)
have been replaced with Supabase queries so the pipeline only makes one
external call (Open Targets) before consulting the curated local database.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.api import disgenet, supabase_matcher
from app.core.evidence_scoring import EvidenceChain, compute_evidence_score, rank_evidence_scores
from app.models import (
    AutomationRunRequest,
    AutomationRunResponse,
    CompoundGeneInteraction,
    DiseaseGeneAssociation,
    EvidenceScore,
    FoodCompoundMapping,
    GenePathwayMapping,
)

logger = logging.getLogger(__name__)


async def run_pipeline(request: AutomationRunRequest) -> AutomationRunResponse:
    """Execute the full automation pipeline for a given disease."""
    import uuid

    run_id = str(uuid.uuid4())
    logger.info(
        "Starting pipeline run %s for disease %s (%s)",
        run_id,
        request.disease_name,
        request.disease_id,
    )

    # Step 1: Disease → Genes (Open Targets)
    genes = await disgenet.get_disease_gene_associations(
        disease_id=request.disease_id,
        min_score=request.min_gene_score,
        limit=request.max_genes,
    )
    logger.info("Found %d genes for disease %s", len(genes), request.disease_id)

    if not genes:
        return _empty_response(run_id, request)

    gene_symbols = [g.gene_symbol for g in genes]

    # Step 2: Genes → Pathways & Compounds (concurrent Supabase queries)
    all_pathways, all_compounds = await asyncio.gather(
        _fetch_pathways(gene_symbols),
        _fetch_compounds(gene_symbols),
    )

    logger.info(
        "Found %d pathways, %d compounds", len(all_pathways), len(all_compounds)
    )

    if not all_compounds:
        return _empty_response(run_id, request, genes, all_pathways)

    # Step 3: Compounds → Foods (single batched Supabase query)
    unique_compound_names = list({c.compound_name for c in all_compounds if c.compound_name})
    all_foods = await _fetch_foods(unique_compound_names)

    logger.info("Found %d food mappings", len(all_foods))

    # Step 4: Assemble evidence chains and compute scores
    evidence_scores = _build_scores(genes, all_pathways, all_compounds, all_foods)
    ranked = rank_evidence_scores(evidence_scores)

    return AutomationRunResponse(
        run_id=run_id,
        disease_id=request.disease_id,
        disease_name=request.disease_name,
        genes_found=len(genes),
        pathways_found=len(all_pathways),
        compounds_found=len(all_compounds),
        foods_found=len(all_foods),
        evidence_scores=ranked,
        status="completed",
    )


async def _fetch_pathways(gene_symbols: list[str]) -> list[GenePathwayMapping]:
    try:
        return await supabase_matcher.get_pathways_for_genes(gene_symbols)
    except Exception as exc:
        logger.warning("Supabase pathway fetch failed: %s", exc)
        return []


async def _fetch_compounds(gene_symbols: list[str]) -> list[CompoundGeneInteraction]:
    try:
        return await supabase_matcher.match_genes_to_compounds(gene_symbols)
    except Exception as exc:
        logger.warning("Supabase compound fetch failed: %s", exc)
        return []


async def _fetch_foods(compound_names: list[str]) -> list[FoodCompoundMapping]:
    try:
        return await supabase_matcher.get_foods_for_compounds(compound_names)
    except Exception as exc:
        logger.warning("Supabase food fetch failed: %s", exc)
        return []


def _build_scores(
    genes: list[DiseaseGeneAssociation],
    pathways: list[GenePathwayMapping],
    compounds: list[CompoundGeneInteraction],
    foods: list[FoodCompoundMapping],
) -> list[EvidenceScore]:
    """Cross-join gene → pathway → compound → food and score each chain.

    Joins are keyed on gene_symbol (not gene_id) because Open Targets returns
    Ensembl IDs as gene_id while Supabase records carry their own UUIDs there.
    Gene symbol is a stable, shared key across both sources.
    """
    gene_map: dict[str, DiseaseGeneAssociation] = {g.gene_symbol: g for g in genes}
    pathway_by_gene: dict[str, list[GenePathwayMapping]] = {}
    compound_by_gene: dict[str, list[CompoundGeneInteraction]] = {}
    food_by_compound: dict[str, list[FoodCompoundMapping]] = {}

    for p in pathways:
        pathway_by_gene.setdefault(p.gene_symbol, []).append(p)
    for c in compounds:
        compound_by_gene.setdefault(c.gene_symbol, []).append(c)
    for f in foods:
        food_by_compound.setdefault(f.compound_name.lower(), []).append(f)

    scores: list[EvidenceScore] = []

    for gene_symbol, gene in gene_map.items():
        gene_pathways = pathway_by_gene.get(gene_symbol, [])
        gene_compounds = compound_by_gene.get(gene_symbol, [])

        # Use a placeholder pathway if none found in curated database
        if not gene_pathways:
            gene_pathways = [
                GenePathwayMapping(
                    gene_id=gene.gene_id,
                    gene_symbol=gene_symbol,
                    pathway_id="",
                    pathway_name="Unknown",
                )
            ]

        for compound in gene_compounds:
            compound_foods = food_by_compound.get(compound.compound_name.lower(), [])
            if not compound_foods:
                continue

            for pathway in gene_pathways[:2]:   # top 2 pathways per gene
                for food in compound_foods[:3]:  # top 3 foods per compound
                    chain = EvidenceChain(
                        disease_gene=gene,
                        pathway=pathway,
                        compound=compound,
                        food=food,
                    )
                    try:
                        scores.append(compute_evidence_score(chain))
                    except Exception as exc:
                        logger.warning("Score computation failed: %s", exc)

    return scores


def _empty_response(
    run_id: str,
    request: AutomationRunRequest,
    genes: list[Any] | None = None,
    pathways: list[Any] | None = None,
) -> AutomationRunResponse:
    return AutomationRunResponse(
        run_id=run_id,
        disease_id=request.disease_id,
        disease_name=request.disease_name,
        genes_found=len(genes) if genes else 0,
        pathways_found=len(pathways) if pathways else 0,
        compounds_found=0,
        foods_found=0,
        evidence_scores=[],
        status="completed_no_results",
    )
