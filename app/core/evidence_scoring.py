"""Automated evidence scoring engine.

Computes a composite score (0–100) for a disease → food recommendation
based on the strength of the evidence chain:
  Disease → Gene (DisGeNET score)
  Gene → Pathway (Reactome coverage)
  Pathway → Compound → Food (ChEMBL activity + USDA presence)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.models import (
    CompoundGeneInteraction,
    DiseaseGeneAssociation,
    EvidenceLevel,
    EvidenceScore,
    FoodCompoundMapping,
    GenePathwayMapping,
)

logger = logging.getLogger(__name__)

# Weight coefficients (must sum to 1.0)
_W_DISEASE_GENE = 0.35
_W_PATHWAY = 0.25
_W_COMPOUND = 0.25
_W_FOOD = 0.15


@dataclass
class EvidenceChain:
    disease_gene: DiseaseGeneAssociation
    pathway: GenePathwayMapping
    compound: CompoundGeneInteraction
    food: FoodCompoundMapping


def _disease_gene_sub_score(association: DiseaseGeneAssociation) -> float:
    """Normalise DisGeNET score (0–1) → (0–100)."""
    return association.score * 100.0


def _pathway_sub_score(pathway: GenePathwayMapping) -> float:
    """Pathway coverage is binary for now; could be enriched with p-values later."""
    return 100.0 if pathway.pathway_id else 0.0


def _compound_sub_score(compound: CompoundGeneInteraction) -> float:
    """Score based on pChEMBL / IC50 activity value presence."""
    if compound.activity_value is None:
        return 40.0  # compound exists but no quantitative data
    # IC50-style lower is better; pChEMBL-style higher is better
    # We use a sigmoid-like normalisation for IC50 in nM:
    # score = 100 / (1 + (value / 1000))  — 1 µM midpoint
    if compound.activity_units and "nm" in compound.activity_units.lower():
        return min(100.0, 100.0 / (1.0 + compound.activity_value / 1000.0))
    return 60.0  # activity present but units unknown


def _food_sub_score(food: FoodCompoundMapping) -> float:
    """Score based on compound amount in food (if available)."""
    if food.compound_amount is None:
        return 50.0  # food found but quantity unknown
    return min(100.0, food.compound_amount)  # capped at 100 arbitrary units


def _level_from_score(score: float) -> EvidenceLevel:
    if score >= 70:
        return EvidenceLevel.high
    if score >= 45:
        return EvidenceLevel.moderate
    if score >= 20:
        return EvidenceLevel.low
    return EvidenceLevel.insufficient


def _build_reasoning(chain: EvidenceChain, score: float) -> str:
    return (
        f"Disease-gene association score: {chain.disease_gene.score:.2f} "
        f"(DisGeNET). Gene {chain.disease_gene.gene_symbol} participates in "
        f"pathway '{chain.pathway.pathway_name}' (Reactome). Compound "
        f"'{chain.compound.compound_name}' modulates this gene "
        f"(ChEMBL activity={chain.compound.activity_value} "
        f"{chain.compound.activity_units or 'N/A'}). "
        f"Food '{chain.food.food_name}' contains this compound "
        f"({chain.food.compound_amount} {chain.food.compound_unit or 'N/A'}). "
        f"Composite evidence score: {score:.1f}/100."
    )


def compute_evidence_score(chain: EvidenceChain) -> EvidenceScore:
    """Compute a composite EvidenceScore for a complete disease→food chain."""
    sub_scores = {
        "disease_gene": _disease_gene_sub_score(chain.disease_gene),
        "pathway": _pathway_sub_score(chain.pathway),
        "compound": _compound_sub_score(chain.compound),
        "food": _food_sub_score(chain.food),
    }

    composite = (
        sub_scores["disease_gene"] * _W_DISEASE_GENE
        + sub_scores["pathway"] * _W_PATHWAY
        + sub_scores["compound"] * _W_COMPOUND
        + sub_scores["food"] * _W_FOOD
    )

    logger.debug("Sub-scores for chain: %s → composite=%.1f", sub_scores, composite)

    level = _level_from_score(composite)
    reasoning = _build_reasoning(chain, composite)

    return EvidenceScore(
        disease_id=chain.disease_gene.disease_id,
        disease_name=chain.disease_gene.disease_name,
        food_id=chain.food.food_id,
        food_name=chain.food.food_name,
        compound_name=chain.compound.compound_name,
        gene_symbol=chain.disease_gene.gene_symbol,
        pathway_name=chain.pathway.pathway_name,
        score=round(composite, 2),
        evidence_level=level,
        reasoning=reasoning,
    )


def rank_evidence_scores(scores: list[EvidenceScore]) -> list[EvidenceScore]:
    """Return scores sorted by composite score descending, deduped by food_id."""
    seen: set[str] = set()
    unique: list[EvidenceScore] = []
    for score in sorted(scores, key=lambda s: s.score, reverse=True):
        if score.food_id not in seen:
            seen.add(score.food_id)
            unique.append(score)
    return unique
