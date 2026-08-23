"""Disease → Gene → Pathway → Phytochemical → Fruit/Vegetable pipeline.

Flow:
  1. Open Targets         →  disease-associated genes
  2. KEGG (concurrent)    →  pathways for each gene
  3. CTD snapshot         →  phytochemical-gene interactions (filtered to dietary)
  4. Supabase lookup      →  fruits/vegetables for each phytochemical
  5. Offline grading      →  count unique PMIDs per pair, map to A/B/C grade
  6. Sort by grade + count, return top N recommendations
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from app.api import chembl, ctd_api, disease_synonyms, disgenet, kegg, pubmed_fallback
from app.data import infectious_disease_compounds
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
_MAX_PHYTOCHEMICALS = 30          # ranked by aggregated CTD publication_count
# How many ranked chemicals to test for a fruit/vegetable mapping before
# settling on the top _MAX_PHYTOCHEMICALS. Since the CTD snapshot grew from ~80
# to ~1,700 compounds, the highest-publication chemicals are often ones we have
# no food source for; checking only the top 30 would let them crowd out the
# compounds that can actually become a recommendation.
_FRUIT_LOOKUP_CANDIDATES = 300
_MAX_PATHWAYS_PER_GENE = 2
_MAX_PAIRS_PER_RUN = 20           # cap on (chemical, gene) pairs scored per run
_DEFAULT_TOP_RESULTS = 20

# Evidence-grade thresholds calibrated for CTD's curated PMID counts.
# CTD counts are smaller than PubMed live search counts because CTD only
# stores literature directly cited for a curated interaction.
_GRADE_A_MIN = 10
_GRADE_B_MIN = 3
_GRADE_C_MIN = 1
_SAMPLE_CITATIONS = 3             # PMIDs to surface per recommendation

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

    # ── Step 1: Disease → Genes (Open Targets, supplemented by DisGeNET) ──────
    genes = await disgenet.get_disease_gene_associations(
        disease_id=request.disease_id,
        min_score=request.min_gene_score,
        limit=request.max_genes,
    )
    logger.info(
        "Open Targets returned %d gene(s) for disease %s", len(genes), request.disease_id
    )

    # ── Step 1b: Recovery ladder for conditions Open Targets scores poorly ────
    # Pathogen-caused diseases often have associations that all sit below the
    # default 0.3 floor (typhoid fever: 284 targets, best score 0.05), so an
    # unmodified run reports "0 genes" for a disease Open Targets does cover.
    provenance = _Provenance()
    if not genes:
        genes, provenance = await _recover_genes(request)

    genes = await _merge_additional_genes(genes, request)
    if not genes:
        # Nothing gene-shaped anywhere — try the literature directly.
        return await _literature_only_response(run_id, request)

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

    # ── Step 3b: ChEMBL fallback when the CTD snapshot has nothing ────────────
    if not ctd_rows:
        logger.info("CTD returned no rows — falling back to ChEMBL for %d gene(s)", len(gene_symbols))
        ctd_rows = await _fetch_chembl_fallback(gene_symbols)
        if not ctd_rows:
            return _empty_response(run_id, request, genes=genes, pathways=pathways)

    # ── Step 4: Phytochemicals → Fruits/Vegetables (Supabase) ─────────────────
    unique_phytochemicals = list({row["chemical_name"] for row in ctd_rows})

    # Rank by CTD pub_count, then keep the best _MAX_PHYTOCHEMICALS that we can
    # actually name a food source for. Ranking and capping in one step would
    # spend the whole budget on well-studied compounds with no food mapping.
    ranked_chems = _rank_phytochemicals(ctd_rows)
    candidate_names = [name for name, _ in ranked_chems[:_FRUIT_LOOKUP_CANDIDATES]]

    candidate_fruits = await _fetch_fruits(candidate_names)
    fruits_map = {
        name: candidate_fruits[name]
        for name in candidate_names
        if candidate_fruits.get(name)
    }
    if len(fruits_map) > _MAX_PHYTOCHEMICALS:
        kept = list(fruits_map)[:_MAX_PHYTOCHEMICALS]
        fruits_map = {name: fruits_map[name] for name in kept}
    logger.info(
        "Phytochemicals with fruit mappings: %d kept from %d candidate(s) (%d ranked)",
        len(fruits_map),
        len(candidate_names),
        len(ranked_chems),
    )

    # Drop any CTD rows whose chemical didn't make the cap or has no fruit mapping.
    relevant_rows = [
        row
        for row in ctd_rows
        if row["chemical_name"] in fruits_map and row["gene_symbol"] in gene_by_symbol
    ]

    # ── Step 5: Offline evidence grading from CTD-stored PMIDs ────────────────
    # Aggregate unique PMIDs per (chemical, gene) pair across all CTD interaction
    # rows (CTD lists each interaction-action as its own row, often citing
    # overlapping literature — set-union avoids double-counting).
    pair_pmids: dict[tuple[str, str], set[str]] = {}
    for r in relevant_rows:
        key = (r["chemical_name"], r["gene_symbol"])
        pair_pmids.setdefault(key, set()).update(r.get("pmids") or [])

    # Rank pairs by unique-PMID count, cap at _MAX_PAIRS_PER_RUN.
    ranked_pairs = sorted(pair_pmids.items(), key=lambda kv: len(kv[1]), reverse=True)
    top_pairs = ranked_pairs[:_MAX_PAIRS_PER_RUN]
    top_pair_set = {pair for pair, _ in top_pairs}

    logger.info(
        "Grading %d / %d unique (phytochemical, gene) pair(s) offline from CTD PMIDs",
        len(top_pair_set),
        len(pair_pmids),
    )
    grades = _grade_offline(top_pairs)

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
        data_source=provenance.data_source,
        evidence_note=provenance.evidence_note,
        disclaimer=provenance.disclaimer,
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
        rows = await ctd_api.get_chemicals_for_genes(gene_symbols)
    except Exception as exc:
        logger.warning("CTD fetch failed: %s", exc)
        return []
    for row in rows:
        row.setdefault("source", "CTD")
    return rows


@dataclass
class _Provenance:
    """How a run's genes were obtained, for disclosure in the response."""

    data_source: str = "open_targets"
    evidence_note: str = ""
    disclaimer: str = ""


async def _recover_genes(
    request: AutomationRunRequest,
) -> tuple[list[DiseaseGeneAssociation], _Provenance]:
    """Ladder of increasingly indirect ways to obtain genes for a disease.

    Ordered by how far each step strays from what the caller asked about:
      1. same disease, lower score floor  — still its own data
      2. a synonym's disease entity       — a related condition, disclosed
      3. curated host-response genes      — literature for the infection type
    """
    name = request.disease_name or ""

    # 1. Same disease, weaker floor.
    genes, floor = await disease_synonyms.retry_with_lower_threshold(
        request.disease_id, request.min_gene_score, request.max_genes
    )
    if genes:
        return genes, _Provenance(
            data_source="open_targets_low_confidence",
            evidence_note=(
                f"Open Targets has no associations for this condition above the usual "
                f"confidence threshold, so results use its weaker associations "
                f"(score ≥ {floor:g})."
            ),
        )

    # 2. A related condition.
    genes, label = await disease_synonyms.try_disease_synonyms(
        name, request.min_gene_score, request.max_genes, exclude_id=request.disease_id
    )
    if genes:
        return genes, _Provenance(
            data_source="open_targets_synonym",
            evidence_note=f"Results based on related condition: {label}",
        )

    # 3. Curated host-response genes for this infection type.
    curated = infectious_disease_compounds.lookup(name)
    if curated:
        key, entry = curated
        associations: list[DiseaseGeneAssociation] = []
        for symbol in entry["genes"]:
            try:
                associations.append(
                    DiseaseGeneAssociation(
                        disease_id=request.disease_id,
                        disease_name=name,
                        gene_id="",
                        gene_symbol=symbol,
                        score=0.0,
                        source="curated_literature",
                    )
                )
            except Exception:
                logger.warning("Skipping malformed curated gene symbol: %r", symbol)
        if associations:
            logger.info(
                "Using curated host-response genes for %r (matched %r): %s",
                name,
                key,
                [a.gene_symbol for a in associations],
            )
            meta = infectious_disease_compounds.response_metadata(entry)
            return associations, _Provenance(
                data_source=meta["data_source"],
                evidence_note=meta["evidence_note"],
                disclaimer=meta["disclaimer"],
            )

    return [], _Provenance()


async def _literature_only_response(
    run_id: str,
    request: AutomationRunRequest,
) -> AutomationRunResponse:
    """Last resort: compounds studied against this disease in PubMed.

    No gene chain is claimed. Every compound here is backed by a real search
    hit and carries its PMIDs, so the reader can check it.
    """
    name = request.disease_name or ""
    rows = await _fetch_pubmed_fallback(name)
    if not rows:
        return _empty_response(run_id, request)

    ranked = _rank_phytochemicals(rows)
    candidates = [n for n, _ in ranked[:_FRUIT_LOOKUP_CANDIDATES]]
    found = await _fetch_fruits(candidates)
    fruits_map = {n: found[n] for n in candidates if found.get(n)}
    if not fruits_map:
        return _empty_response(run_id, request)

    pair_pmids: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        if row["chemical_name"] not in fruits_map:
            continue
        key = (row["chemical_name"], row["gene_symbol"])
        pair_pmids.setdefault(key, set()).update(row.get("pmids") or [])

    grades = _grade_offline(sorted(pair_pmids.items(), key=lambda kv: len(kv[1]), reverse=True))
    recommendations = _build_recommendations(
        ctd_rows=[r for r in rows if r["chemical_name"] in fruits_map],
        fruits_map=fruits_map,
        grades=grades,
        pathway_by_gene={},
    )
    recommendations = _sort_and_trim(recommendations, top_n=_DEFAULT_TOP_RESULTS)

    disclaimer = infectious_disease_compounds.TREATMENT_DISCLAIMER
    return AutomationRunResponse(
        run_id=run_id,
        disease_id=request.disease_id,
        disease_name=request.disease_name,
        genes_found=0,
        pathways_found=0,
        compounds_found=len({r["chemical_name"] for r in rows}),
        foods_found=len({r.fruit_vegetable for r in recommendations}),
        evidence_scores=[],
        recommendations=recommendations,
        status="completed_literature_only",
        data_source="pubmed_literature",
        evidence_note=(
            "No gene-level data was available for this condition, so these "
            "compounds come from published studies pairing them with it. A "
            "published study means the pairing has been investigated, not that "
            "it is effective."
        ),
        disclaimer=disclaimer,
    )


async def _fetch_pubmed_fallback(disease_name: str) -> list[dict[str, Any]]:
    try:
        return await pubmed_fallback.get_phytochemicals_for_disease(disease_name)
    except Exception as exc:  # noqa: BLE001 - the fallback must not break the run
        logger.warning("PubMed fallback failed for %r: %s", disease_name, exc)
        return []


async def _fetch_chembl_fallback(gene_symbols: list[str]) -> list[dict[str, Any]]:
    """Natural-product compounds from ChEMBL, in the CTD row shape."""
    try:
        rows = await chembl.get_natural_compounds_for_genes(gene_symbols)
    except Exception as exc:  # noqa: BLE001 - the fallback must not break the run
        logger.warning("ChEMBL fallback failed: %s", exc)
        return []
    logger.info("ChEMBL fallback produced %d row(s)", len(rows))
    return rows


async def _merge_additional_genes(
    genes: list[DiseaseGeneAssociation],
    request: AutomationRunRequest,
) -> list[DiseaseGeneAssociation]:
    """Append DisGeNET genes that Open Targets did not already return.

    Supplementary genes carry score 0.0 and source "disgenet" so downstream
    ranking still prefers Open Targets' scored associations.
    """
    disease_name = (request.disease_name or "").strip()
    if not disease_name:
        return genes

    try:
        extra_symbols = await disgenet.get_additional_genes(disease_name)
    except Exception as exc:  # noqa: BLE001 - a supplement must never break the run
        logger.warning("DisGeNET merge failed: %s", exc)
        return genes

    if not extra_symbols:
        return genes

    known = {g.gene_symbol.strip().upper() for g in genes}
    merged = list(genes)
    added = 0
    for symbol in extra_symbols:
        if symbol in known:
            continue
        known.add(symbol)
        try:
            merged.append(
                DiseaseGeneAssociation(
                    disease_id=request.disease_id,
                    disease_name=disease_name,
                    gene_id="",
                    gene_symbol=symbol,
                    score=0.0,
                    source="disgenet",
                )
            )
            added += 1
        except Exception:
            logger.warning("Skipping malformed DisGeNET gene symbol: %r", symbol)

    logger.info(
        "Genes after DisGeNET merge: %d (%d from Open Targets, %d added)",
        len(merged),
        len(genes),
        added,
    )
    return merged


async def _fetch_fruits(phytochemical_names: list[str]) -> dict[str, list[str]]:
    try:
        return await get_fruits_for_phytochemicals(phytochemical_names)
    except Exception as exc:
        logger.warning("Supabase fruit lookup failed: %s", exc)
        return {}


def _grade_offline(
    ranked_pairs: list[tuple[tuple[str, str], set[str]]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Compute evidence grades from CTD-stored PMIDs — no network calls.

    `ranked_pairs` is an iterable of ((chem, gene), {pmid, pmid, ...}) tuples.
    Returns the same dict shape the live PubMed grader produced, so downstream
    code (build_recommendations) is unchanged.
    """
    grades: dict[tuple[str, str], dict[str, Any]] = {}
    for pair, pmids in ranked_pairs:
        count = len(pmids)
        if count >= _GRADE_A_MIN:
            grade = "A"
        elif count >= _GRADE_B_MIN:
            grade = "B"
        elif count >= _GRADE_C_MIN:
            grade = "C"
        else:
            grade = "None"

        # Stable sample selection: take the lexicographically smallest PMIDs so
        # repeat runs return the same citations for the same pair.
        sample = [f"PMID {pmid}" for pmid in sorted(pmids)[:_SAMPLE_CITATIONS]]
        grades[pair] = {
            "publication_count": count,
            "evidence_grade": grade,
            "sample_citations": sample,
        }
    return grades


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
                    source=row.get("source", "CTD"),
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
