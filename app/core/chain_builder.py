"""Disease → Gene → Pathway → Compound → Food pipeline.

Orchestrates calls to the four external APIs and assembles
EvidenceChain objects ready for scoring.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.api import chembl, disgenet, reactome, usda
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


async def _fetch_pathways(gene: DiseaseGeneAssociation) -> list[GenePathwayMapping]:
    try:
        return await reactome.get_pathways_for_gene(gene.gene_symbol, gene.gene_id)
    except Exception as exc:
        logger.warning("Pathway fetch failed for %s: %s", gene.gene_symbol, exc)
        return []


async def _fetch_compounds(gene: DiseaseGeneAssociation) -> list[CompoundGeneInteraction]:
    try:
        return await chembl.get_compounds_for_gene(gene.gene_symbol, gene.gene_id)
    except Exception as exc:
        logger.warning("Compound fetch failed for %s: %s", gene.gene_symbol, exc)
        return []


async def _fetch_foods(compound_name: str) -> list[FoodCompoundMapping]:
    try:
        return await usda.search_foods_by_compound(compound_name)
    except Exception as exc:
        logger.warning("Food fetch failed for compound %s: %s", compound_name, exc)
        return []


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

    # Step 1: Disease → Genes
    genes = await disgenet.get_disease_gene_associations(
        disease_id=request.disease_id,
        min_score=request.min_gene_score,
        limit=request.max_genes,
    )
    logger.info("Found %d genes for disease %s", len(genes), request.disease_id)

    if not genes:
        return _empty_response(run_id, request)

    # Step 2: Genes → Pathways & Compounds (concurrent per gene)
    pathway_tasks = [_fetch_pathways(g) for g in genes]
    compound_tasks = [_fetch_compounds(g) for g in genes]

    pathway_results, compound_results = await asyncio.gather(
        asyncio.gather(*pathway_tasks),
        asyncio.gather(*compound_tasks),
    )

    all_pathways: list[GenePathwayMapping] = [p for ps in pathway_results for p in ps]
    all_compounds: list[CompoundGeneInteraction] = [c for cs in compound_results for c in cs]

    logger.info(
        "Found %d pathways, %d compounds", len(all_pathways), len(all_compounds)
    )

    if not all_compounds:
        return _empty_response(run_id, request, genes, all_pathways)

    # Step 3: Compounds → Foods (concurrent)
    # Guard: exclude any compound whose name is still a bare database ID
    # (e.g. "CHEMBL360610").  These slip through when ChEMBL has no
    # molecule_pref_name and would produce zero USDA results.
    unique_compound_names = list({
        c.compound_name
        for c in all_compounds
        if not c.compound_name.upper().startswith("CHEMBL")
    })
    if len(unique_compound_names) < len({c.compound_name for c in all_compounds}):
        logger.warning(
            "Dropped %d compound(s) with no human-readable name before USDA search",
            len({c.compound_name for c in all_compounds}) - len(unique_compound_names),
        )
    food_tasks = [_fetch_foods(name) for name in unique_compound_names]
    food_results: list[list[FoodCompoundMapping]] = await asyncio.gather(*food_tasks)
    all_foods: list[FoodCompoundMapping] = [f for fs in food_results for f in fs]

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


def _build_scores(
    genes: list[DiseaseGeneAssociation],
    pathways: list[GenePathwayMapping],
    compounds: list[CompoundGeneInteraction],
    foods: list[FoodCompoundMapping],
) -> list[EvidenceScore]:
    """Cross-join gene → pathway → compound → food and score each chain."""
    gene_map: dict[str, DiseaseGeneAssociation] = {g.gene_id: g for g in genes}
    pathway_by_gene: dict[str, list[GenePathwayMapping]] = {}
    compound_by_gene: dict[str, list[CompoundGeneInteraction]] = {}
    food_by_compound: dict[str, list[FoodCompoundMapping]] = {}

    for p in pathways:
        pathway_by_gene.setdefault(p.gene_id, []).append(p)
    for c in compounds:
        compound_by_gene.setdefault(c.gene_id, []).append(c)
    for f in foods:
        food_by_compound.setdefault(f.compound_name.lower(), []).append(f)

    scores: list[EvidenceScore] = []

    for gene_id, gene in gene_map.items():
        gene_pathways = pathway_by_gene.get(gene_id, [])
        gene_compounds = compound_by_gene.get(gene_id, [])

        # Use a placeholder pathway if none found
        if not gene_pathways:
            gene_pathways = [
                GenePathwayMapping(
                    gene_id=gene_id,
                    gene_symbol=gene.gene_symbol,
                    pathway_id="",
                    pathway_name="Unknown",
                )
            ]

        for compound in gene_compounds:
            compound_foods = food_by_compound.get(compound.compound_name.lower(), [])
            if not compound_foods:
                continue

            for pathway in gene_pathways[:2]:  # limit to top 2 pathways per gene
                for food in compound_foods[:3]:  # limit to top 3 foods per compound
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
