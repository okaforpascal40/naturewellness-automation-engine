"""Disease → Gene → Pathway → Phytochemical → Fruit/Vegetable pipeline.

Flow:
  1. Open Targets         →  disease-associated genes
  2. KEGG (concurrent)    →  pathways for each gene
  3. CTD batchQuery       →  phytochemical-gene interactions (filtered to dietary)
  4. Supabase lookup      →  fruits/vegetables for each phytochemical
  5. PubMed E-utilities   →  publication count + sample citations per phytochemical-gene pair
  6. Sort by grade + count, return top N recommendations
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from app.api import ctd_api, disgenet, kegg, pubmed_api
from app.database import get_fruits_for_phytochemicals
from app.models import (
    AutomationRunRequest,
    AutomationRunResponse,
    DiseaseGeneAssociation,
    FruitRecommendation,
    GenePathwayMapping,
)

logger = logging.getLogger(__name__)

# Caps to keep one pipeline run bounded.
_MAX_PHYTOCHEMICALS = 30          # ranked by CTD publication_count before PubMed
_MAX_PATHWAYS_PER_GENE = 2
_PUBMED_CONCURRENCY = 3           # NCBI: 3 req/s without API key, 10 with
_DEFAULT_TOP_RESULTS = 20

# Map letter grade to a sort weight (higher = better).
_GRADE_RANK = {"A": 3, "B": 2, "C": 1, "None": 0}


async def run_pipeline(request: AutomationRunRequest) -> AutomationRunResponse:
    """Execute the CTD + PubMed pipeline for a given disease."""
    run_id = str(uuid.uuid4())
    logger.info(
        "Starting pipeline run %s for disease %s (%s)",
        run_id,
        request.disease_name or "?",
        request.disease_id,
    )

    # ── Step 1: Disease → Genes (Open Targets) ────────────────────────────────
    genes = await disgenet.get_disease_gene_associations(
        disease_id=request.disease_id,
        min_score=request.min_gene_score,
        limit=request.max_genes,
    )
    logger.info("Found %d genes for disease %s", len(genes), request.disease_id)
    if not genes:
        return _empty_response(run_id, request)

    gene_symbols = [g.gene_symbol for g in genes]
    gene_by_symbol: dict[str, DiseaseGeneAssociation] = {g.gene_symbol: g for g in genes}

    # ── Step 2 & 3 in parallel: Pathways (KEGG) and Phytochemicals (CTD) ──────
    pathways, ctd_rows = await asyncio.gather(
        _fetch_pathways(genes),
        _fetch_phytochemicals(gene_symbols),
    )
    logger.info(
        "Found %d KEGG pathway rows and %d CTD interaction rows",
        len(pathways),
        len(ctd_rows),
    )

    if not ctd_rows:
        return _empty_response(run_id, request, genes=genes, pathways=pathways)

    # ── Step 4: Phytochemicals → Fruits/Vegetables (Supabase) ─────────────────
    unique_phytochemicals = list({row["chemical_name"] for row in ctd_rows})

    # Rank phytochemicals by CTD pub_count and cap before the heavy steps.
    ranked_chems = _rank_phytochemicals(ctd_rows)[:_MAX_PHYTOCHEMICALS]
    capped_names = [name for name, _ in ranked_chems]

    fruits_map = await _fetch_fruits(capped_names)
    logger.info(
        "Phytochemicals with fruit mappings: %d / %d",
        len(fruits_map),
        len(capped_names),
    )

    # Drop any CTD rows whose chemical didn't make the cap or has no fruit mapping.
    relevant_rows = [
        row
        for row in ctd_rows
        if row["chemical_name"] in fruits_map and row["gene_symbol"] in gene_by_symbol
    ]

    # ── Step 5: PubMed evidence per (phytochemical, gene) pair ────────────────
    # Rank pairs by CTD publication_count and cap to pubmed_api.MAX_PAIRS_PER_RUN
    # so total response time stays bounded even for high-coverage diseases.
    pair_counts: dict[tuple[str, str], int] = {}
    for r in relevant_rows:
        key = (r["chemical_name"], r["gene_symbol"])
        pair_counts[key] = pair_counts.get(key, 0) + int(r.get("publication_count", 0))

    ranked_pairs = sorted(pair_counts.items(), key=lambda kv: kv[1], reverse=True)
    top_pair_set = {pair for pair, _ in ranked_pairs[: pubmed_api.MAX_PAIRS_PER_RUN]}

    logger.info(
        "Grading %d / %d unique (phytochemical, gene) pair(s) via PubMed (capped)",
        len(top_pair_set),
        len(pair_counts),
    )
    grades = await _grade_pairs(sorted(top_pair_set))

    # Drop CTD rows whose pair didn't make the cap — they wouldn't be graded.
    relevant_rows = [
        r for r in relevant_rows
        if (r["chemical_name"], r["gene_symbol"]) in top_pair_set
    ]

    # ── Step 6: Build recommendations, sort, return top N ─────────────────────
    pathway_by_gene = _index_pathways(pathways)
    recommendations = _build_recommendations(
        ctd_rows=relevant_rows,
        fruits_map=fruits_map,
        grades=grades,
        pathway_by_gene=pathway_by_gene,
    )
    recommendations = _sort_and_trim(recommendations, top_n=_DEFAULT_TOP_RESULTS)

    return AutomationRunResponse(
        run_id=run_id,
        disease_id=request.disease_id,
        disease_name=request.disease_name,
        genes_found=len(genes),
        pathways_found=len(pathways),
        compounds_found=len(unique_phytochemicals),
        foods_found=len({r.fruit_vegetable for r in recommendations}),
        evidence_scores=[],
        recommendations=recommendations,
        status="completed",
    )


# ── Stage helpers ──────────────────────────────────────────────────────────────


async def _fetch_pathways(
    genes: list[DiseaseGeneAssociation],
) -> list[GenePathwayMapping]:
    """KEGG pathway lookup, one call per gene, run concurrently."""

    async def one(g: DiseaseGeneAssociation) -> list[GenePathwayMapping]:
        try:
            return await kegg.get_pathways_for_gene(g.gene_symbol, g.gene_id)
        except Exception as exc:
            logger.warning("KEGG pathway fetch failed for %s: %s", g.gene_symbol, exc)
            return []

    results = await asyncio.gather(*(one(g) for g in genes))
    return [p for batch in results for p in batch]


async def _fetch_phytochemicals(gene_symbols: list[str]) -> list[dict[str, Any]]:
    try:
        return await ctd_api.get_chemicals_for_genes(gene_symbols)
    except Exception as exc:
        logger.warning("CTD fetch failed: %s", exc)
        return []


async def _fetch_fruits(phytochemical_names: list[str]) -> dict[str, list[str]]:
    try:
        return await get_fruits_for_phytochemicals(phytochemical_names)
    except Exception as exc:
        logger.warning("Supabase fruit lookup failed: %s", exc)
        return {}


async def _grade_pairs(
    pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Run pubmed_api.grade_evidence for each pair with bounded concurrency."""
    semaphore = asyncio.Semaphore(_PUBMED_CONCURRENCY)

    async def grade(pair: tuple[str, str]) -> tuple[tuple[str, str], dict[str, Any]]:
        chem, gene = pair
        async with semaphore:
            try:
                result = await pubmed_api.grade_evidence(chem, gene, sample_size=3)
            except Exception as exc:
                logger.warning("PubMed grading failed for (%s, %s): %s", chem, gene, exc)
                result = {"publication_count": 0, "evidence_grade": "None", "sample_citations": []}
        return pair, result

    results = await asyncio.gather(*(grade(p) for p in pairs))
    return dict(results)


# ── Pure helpers ──────────────────────────────────────────────────────────────


def _rank_phytochemicals(ctd_rows: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Aggregate CTD pub_count per chemical and return descending list."""
    totals: dict[str, int] = {}
    for row in ctd_rows:
        name = row.get("chemical_name", "")
        if not name:
            continue
        totals[name] = totals.get(name, 0) + int(row.get("publication_count", 0))
    return sorted(totals.items(), key=lambda kv: kv[1], reverse=True)


def _index_pathways(
    pathways: list[GenePathwayMapping],
) -> dict[str, list[GenePathwayMapping]]:
    by_gene: dict[str, list[GenePathwayMapping]] = {}
    for p in pathways:
        by_gene.setdefault(p.gene_symbol, []).append(p)
    return by_gene


def _build_recommendations(
    ctd_rows: list[dict[str, Any]],
    fruits_map: dict[str, list[str]],
    grades: dict[tuple[str, str], dict[str, Any]],
    pathway_by_gene: dict[str, list[GenePathwayMapping]],
) -> list[FruitRecommendation]:
    """Cross-join (CTD row × fruit) into one recommendation per fruit/phyto pair.

    Deduplicates so the same (fruit, phytochemical, gene) triple only appears once.
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[FruitRecommendation] = []

    for row in ctd_rows:
        chem = row["chemical_name"]
        gene = row["gene_symbol"]
        interaction = row.get("interaction_type", "") or ""

        evidence = grades.get((chem, gene), {})
        grade = evidence.get("evidence_grade", "None")
        pub_count = int(evidence.get("publication_count", 0))
        citations = evidence.get("sample_citations", []) or []

        gene_pathways = pathway_by_gene.get(gene, [])
        pathway_name = gene_pathways[0].pathway_name if gene_pathways else ""
        # Pin to top pathway per the cap; secondary pathways could be exposed later.
        _ = _MAX_PATHWAYS_PER_GENE  # cap reserved for future per-pathway expansion

        for fruit in fruits_map.get(chem, []):
            key = (fruit, chem, gene)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                FruitRecommendation(
                    fruit_vegetable=fruit,
                    phytochemical=chem,
                    gene_target=gene,
                    interaction_type=interaction,
                    evidence_grade=grade,
                    publication_count=pub_count,
                    sample_citations=citations,
                    pathway=pathway_name,
                )
            )
    return out


def _sort_and_trim(
    recommendations: list[FruitRecommendation],
    top_n: int,
) -> list[FruitRecommendation]:
    """Sort by evidence grade (A>B>C>None) then by publication count, descending."""
    recommendations.sort(
        key=lambda r: (_GRADE_RANK.get(r.evidence_grade, 0), r.publication_count),
        reverse=True,
    )
    return recommendations[:top_n]


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
        recommendations=[],
        status="completed_no_results",
    )
