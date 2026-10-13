"""Plant search — plant name → conditions it may support, with graded evidence.

  POST /plant/search       — plant → phytochemicals → CTD genes
                             → Open Targets diseases (reverse lookup)
                             → offline PMID-count grading (chain_builder thresholds)
  GET  /plant/suggestions  — autocomplete over plant names in phytochemical_sources

A condition's evidence is the union of CTD-curated PMIDs for the
phytochemical-gene interactions that connect the plant to it. Open Targets only
supplies the gene → condition link; it never contributes to the grade.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from app.api import ctd_api, target_diseases
from app.core import plant_lookup

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/plant", tags=["plant"])

# ── Tuning ────────────────────────────────────────────────────────────────────
_MAX_NAME_LENGTH = 100
_MAX_GENES = 15                # genes sent to Open Targets, ranked by PMID count
_TOP_CONDITIONS = 10
_SAMPLE_CITATIONS = 3
_MAX_SUGGESTIONS = 10
_MIN_SUGGESTION_QUERY = 2
_RECOMMENDATION_SOURCE = "academic_literature"

Grade = Literal["A", "B", "C"]


# ── Request / response schemas ────────────────────────────────────────────────
class PlantSearchRequest(BaseModel):
    plant_name: str = Field(..., max_length=_MAX_NAME_LENGTH)

    @field_validator("plant_name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("plant_name must not be empty")
        return stripped


class ConditionEvidence(BaseModel):
    condition_name: str
    condition_id: str | None = None
    gene_targets: list[str] = Field(default_factory=list)
    evidence_grade: Grade
    publication_count: int = 0
    interaction_type: str = ""
    sample_citations: list[str] = Field(default_factory=list)


class PlantSearchResponse(BaseModel):
    plant_name: str
    # phytochemical_sources holds neither a scientific name nor nutrient data;
    # these stay null until a real source backs them.
    scientific_name: str | None = None
    phytochemicals: list[str] = Field(default_factory=list)
    nutrient_content: str | None = None
    conditions_with_evidence: list[ConditionEvidence] = Field(default_factory=list)
    total_conditions: int = 0
    total_publications: int = 0
    recommendation_source: str = _RECOMMENDATION_SOURCE


# ── Endpoints ─────────────────────────────────────────────────────────────────
@router.post(
    "/search",
    response_model=PlantSearchResponse,
    summary="Conditions a plant may support, graded from curated literature",
)
async def search_plant(request: PlantSearchRequest) -> PlantSearchResponse:
    try:
        rows = await plant_lookup.cached_phytochemical_sources()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Plant search: phytochemical_sources unavailable")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Plant lookup error: {exc}",
        ) from exc

    match = plant_lookup.match_plant(rows, request.plant_name, exact_first=True)
    if not match.phytochemicals:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No phytochemical data for plant {request.plant_name!r}.",
        )

    try:
        return await _build_response(
            match.matched_name or request.plant_name, match.phytochemicals
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Plant search pipeline failed for %s", request.plant_name)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Plant search error: {exc}",
        ) from exc


@router.get(
    "/suggestions",
    response_model=list[str],
    summary="Plant names matching a partial query",
)
async def plant_suggestions(q: str = Query("")) -> list[str]:
    query = q.strip().lower()
    # No Query(max_length): the contract is always a 200 array, so pasted text
    # longer than any plant name simply matches nothing.
    if not _MIN_SUGGESTION_QUERY <= len(query) <= _MAX_NAME_LENGTH:
        return []
    try:
        rows = await plant_lookup.cached_phytochemical_sources()
    except Exception as exc:  # noqa: BLE001 - autocomplete must not error the UI
        logger.warning("Plant suggestions unavailable: %s", exc)
        return []

    names = [n for n in plant_lookup.plant_names(rows) if query in n.lower()]
    # Prefix matches first; plant_names is already alphabetical within each group.
    names.sort(key=lambda n: not n.lower().startswith(query))
    return names[:_MAX_SUGGESTIONS]


# ── Pipeline ──────────────────────────────────────────────────────────────────
@dataclass
class _Condition:
    name: str
    condition_id: str
    genes: list[str] = field(default_factory=list)
    pmids: set[str] = field(default_factory=set)


async def _build_response(plant_name: str, phytochemicals: list[str]) -> PlantSearchResponse:
    ctd_rows = await ctd_api.get_genes_for_chemicals(phytochemicals)
    gene_pmids, gene_interaction = _aggregate_genes(ctd_rows)

    ranked = sorted(gene_pmids, key=lambda g: (-len(gene_pmids[g]), g))[:_MAX_GENES]
    links = await target_diseases.get_diseases_for_genes(ranked) if ranked else []

    conditions = _build_conditions(links, ranked, gene_pmids)
    top = conditions[:_TOP_CONDITIONS]

    evidence: list[ConditionEvidence] = []
    shown_pmids: set[str] = set()
    for cond in top:
        shown_pmids |= cond.pmids
        evidence.append(
            ConditionEvidence(
                condition_name=cond.name,
                condition_id=cond.condition_id or None,
                gene_targets=cond.genes,
                # Every kept condition has >= 1 PMID, so the grade is A, B or C.
                evidence_grade=cast(Grade, plant_lookup.grade_from_pmid_count(len(cond.pmids))),
                publication_count=len(cond.pmids),
                # Genes are ordered strongest-first, so this is the best-supported one's.
                interaction_type=gene_interaction.get(cond.genes[0], ""),
                sample_citations=[f"PMID {p}" for p in sorted(cond.pmids)[:_SAMPLE_CITATIONS]],
            )
        )

    return PlantSearchResponse(
        plant_name=plant_name,
        phytochemicals=phytochemicals,
        conditions_with_evidence=evidence,
        total_conditions=len(conditions),
        total_publications=len(shown_pmids),
    )


def _aggregate_genes(
    ctd_rows: list[dict[str, Any]],
) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Unique PMIDs and first interaction type per gene symbol."""
    gene_pmids: dict[str, set[str]] = {}
    gene_interaction: dict[str, str] = {}
    for row in ctd_rows:
        gene = (row.get("gene_symbol") or "").strip().upper()
        if not gene:
            continue
        gene_pmids.setdefault(gene, set()).update(str(p) for p in row.get("pmids") or [])
        if gene not in gene_interaction and row.get("interaction_type"):
            gene_interaction[gene] = row["interaction_type"]
    return gene_pmids, gene_interaction


def _build_conditions(
    links: list[target_diseases.GeneDisease],
    ranked_genes: list[str],
    gene_pmids: dict[str, set[str]],
) -> list[_Condition]:
    """Group gene → disease links into gradeable conditions, strongest first.

    Conditions whose genes carry no PMIDs cannot be graded and are dropped
    rather than shown with an invented grade.
    """
    gene_order = {g: i for i, g in enumerate(ranked_genes)}
    by_id: dict[str, _Condition] = {}
    for link in links:
        gene = link.gene_symbol.upper()
        if gene not in gene_pmids:
            continue
        cond = by_id.setdefault(link.disease_id, _Condition(link.disease_name, link.disease_id))
        if gene not in cond.genes:
            cond.genes.append(gene)
            cond.pmids |= gene_pmids[gene]

    conditions = [c for c in by_id.values() if c.pmids]
    for cond in conditions:
        cond.genes.sort(key=lambda g: gene_order.get(g, len(gene_order)))

    conditions.sort(
        key=lambda c: (
            -plant_lookup.GRADE_RANK.get(plant_lookup.grade_from_pmid_count(len(c.pmids)), 0),
            -len(c.pmids),
            -len(c.genes),
            c.name.lower(),
        )
    )
    return conditions
