"""CamScan endpoints — plant identification and reverse health-benefit lookup.

Two endpoints:
  1. POST /camscan/identify         — image → plant identity (Plant.id)
  2. POST /camscan/health-benefits  — plant → phytochemicals → genes →
                                      pathways → graded evidence associations

The health-benefits flow is the reverse of the main disease→food pipeline:
  plant → phytochemical_sources → phytochemicals
        → ctd_phytochemical_gene_interactions → genes
        → KEGG pathways
        → offline PMID-count grading (same thresholds as chain_builder)
        → conditions the plant may support (from stored evidence_scores)
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api import ctd_api, kegg, plantid_api
from app.config import get_settings
from app.database import get_supabase
from app.models import GenePathwayMapping

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/camscan", tags=["camscan"])

# ── Tuning ────────────────────────────────────────────────────────────────────
_TOP_GENES = 10
_SAMPLE_CITATIONS = 3
# Offline evidence-grade thresholds — mirror app/core/chain_builder.py, which
# grades CTD-curated PMID counts (smaller than live PubMed search counts).
_GRADE_A_MIN = 10
_GRADE_B_MIN = 3
_GRADE_C_MIN = 1
_GRADE_RANK = {"A": 3, "B": 2, "C": 1, "None": 0}

_POTENTIAL_BENEFIT = "Evidence-Based Biological Association"
_RECOMMENDATION_SOURCE = "academic_literature"


# ── Request / response schemas ────────────────────────────────────────────────
class IdentifyRequest(BaseModel):
    image: str = Field(..., description="Base64-encoded image (bare or data URI)")


class HealthBenefitsRequest(BaseModel):
    plant_name: str
    scientific_name: str = ""


class HealthAssociation(BaseModel):
    gene: str
    pathway: str = ""
    evidence_grade: str = "None"
    publication_count: int = 0
    interaction_type: str = ""
    sample_citations: list[str] = Field(default_factory=list)
    potential_health_benefit: str = _POTENTIAL_BENEFIT


class HealthBenefitsResponse(BaseModel):
    plant_name: str
    scientific_name: str = ""
    phytochemicals: list[str] = Field(default_factory=list)
    health_associations: list[HealthAssociation] = Field(default_factory=list)
    conditions_supported: list[str] = Field(default_factory=list)
    total_publications: int = 0
    recommendation_source: str = _RECOMMENDATION_SOURCE
    # Populated when we identify the plant but hold no phytochemical data for it.
    status: str = "ok"
    message: str | None = None


# ── Endpoints ─────────────────────────────────────────────────────────────────
@router.post("/identify", summary="Identify a plant from an image")
async def identify(request: IdentifyRequest) -> dict[str, Any]:
    """Identify a plant from a base64 image via the Plant.id API."""
    settings = get_settings()
    try:
        result = await plantid_api.identify_plant(
            image_base64=request.image,
            api_key=settings.plantid_api_key,
            api_url=settings.plantid_api_url,
        )
    except RuntimeError as exc:
        logger.warning("CamScan identify failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    except Exception as exc:  # noqa: BLE001 — surface an opaque 500 with context
        logger.exception("CamScan identify unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error during identification: {exc}",
        ) from exc
    return result


@router.post(
    "/health-benefits",
    response_model=HealthBenefitsResponse,
    summary="Reverse-lookup evidence-based health associations for a plant",
)
async def health_benefits(request: HealthBenefitsRequest) -> HealthBenefitsResponse:
    """Map a plant to graded gene/pathway evidence via phytochemicals."""
    plant_name = request.plant_name.strip()
    if not plant_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="plant_name is required.",
        )

    try:
        return await _build_health_benefits(plant_name, request.scientific_name.strip())
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("CamScan health-benefits pipeline failed for %s", plant_name)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Health-benefits lookup error: {exc}",
        ) from exc


# ── Pipeline ──────────────────────────────────────────────────────────────────
async def _build_health_benefits(
    plant_name: str, scientific_name: str
) -> HealthBenefitsResponse:
    # a-b. Plant → phytochemicals (phytochemical_sources reverse lookup)
    phytochemicals = await _find_phytochemicals_for_plant(plant_name)
    if not phytochemicals:
        logger.info("CamScan: no phytochemical data for plant %r", plant_name)
        return HealthBenefitsResponse(
            plant_name=plant_name,
            scientific_name=scientific_name,
            status="no_data",
            message=(
                f"We identified {plant_name} but don't have phytochemical "
                "data for it yet."
            ),
        )

    # c. Phytochemicals → CTD gene interactions
    ctd_rows = await ctd_api.get_genes_for_chemicals(phytochemicals)
    if not ctd_rows:
        return HealthBenefitsResponse(
            plant_name=plant_name,
            scientific_name=scientific_name,
            phytochemicals=phytochemicals,
            status="no_data",
            message=(
                f"We found phytochemicals for {plant_name} but no curated gene "
                "interactions yet."
            ),
        )

    # d. Aggregate per gene, rank by unique-PMID count, keep top N.
    gene_pmids: dict[str, set[str]] = {}
    gene_interaction: dict[str, str] = {}
    gene_id_map: dict[str, str] = {}
    for row in ctd_rows:
        gene = row.get("gene_symbol", "")
        if not gene:
            continue
        gene_pmids.setdefault(gene, set()).update(row.get("pmids") or [])
        if gene not in gene_interaction and row.get("interaction_type"):
            gene_interaction[gene] = row["interaction_type"]
        if gene not in gene_id_map and row.get("gene_id"):
            gene_id_map[gene] = row["gene_id"]

    ranked_genes = sorted(gene_pmids.items(), key=lambda kv: len(kv[1]), reverse=True)
    top_genes = ranked_genes[:_TOP_GENES]
    top_gene_symbols = [g for g, _ in top_genes]

    # e & g in parallel: KEGG pathways for genes, and conditions from evidence_scores.
    pathways, conditions = await asyncio.gather(
        _fetch_pathways(top_genes, gene_id_map),
        _fetch_conditions(top_gene_symbols),
    )
    pathway_by_gene = _index_pathways(pathways)

    # f. Grade each gene offline from its unique PMID set; build associations.
    associations: list[HealthAssociation] = []
    total_publications = 0
    for gene, pmids in top_genes:
        count = len(pmids)
        total_publications += count
        grade = _grade(count)
        gene_pathways = pathway_by_gene.get(gene, [])
        pathway_name = gene_pathways[0].pathway_name if gene_pathways else ""
        sample = [f"PMID {pmid}" for pmid in sorted(pmids)[:_SAMPLE_CITATIONS]]
        associations.append(
            HealthAssociation(
                gene=gene,
                pathway=pathway_name,
                evidence_grade=grade,
                publication_count=count,
                interaction_type=gene_interaction.get(gene, ""),
                sample_citations=sample,
            )
        )

    associations.sort(
        key=lambda a: (_GRADE_RANK.get(a.evidence_grade, 0), a.publication_count),
        reverse=True,
    )

    return HealthBenefitsResponse(
        plant_name=plant_name,
        scientific_name=scientific_name,
        phytochemicals=phytochemicals,
        health_associations=associations,
        conditions_supported=conditions,
        total_publications=total_publications,
    )


# ── Stage helpers ─────────────────────────────────────────────────────────────
def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


async def _find_phytochemicals_for_plant(plant_name: str) -> list[str]:
    """Return phytochemicals whose fruit_vegetables list contains this plant.

    Matching is case-insensitive and word-level: a source food matches when it
    equals the plant, or one name's word set is a subset of the other's (so
    "Onion" matches "Red Onion" but "Pea" does not match "Peach").
    """
    client = await get_supabase()
    resp = (
        await client.table("phytochemical_sources")
        .select("phytochemical_name,fruit_vegetables")
        .execute()
    )

    plant_l = plant_name.strip().lower()
    plant_tokens = _tokens(plant_name)
    matches: list[str] = []
    seen: set[str] = set()

    for row in resp.data or []:
        name = (row.get("phytochemical_name") or "").strip()
        if not name or name.lower() in seen:
            continue
        for fv in row.get("fruit_vegetables") or []:
            fvl = str(fv).strip().lower()
            if not fvl:
                continue
            fv_tokens = _tokens(fvl)
            if (
                fvl == plant_l
                or (fv_tokens and fv_tokens <= plant_tokens)
                or (plant_tokens and plant_tokens <= fv_tokens)
            ):
                matches.append(name)
                seen.add(name.lower())
                break

    return matches


async def _fetch_pathways(
    top_genes: list[tuple[str, set[str]]],
    gene_id_map: dict[str, str],
) -> list[GenePathwayMapping]:
    """KEGG pathway lookup per gene, run concurrently (best-effort)."""

    async def one(gene: str) -> list[GenePathwayMapping]:
        try:
            return await kegg.get_pathways_for_gene(gene, gene_id_map.get(gene))
        except Exception as exc:  # noqa: BLE001
            logger.warning("KEGG pathway fetch failed for %s: %s", gene, exc)
            return []

    results = await asyncio.gather(*(one(g) for g, _ in top_genes))
    return [p for batch in results for p in batch]


async def _fetch_conditions(gene_symbols: list[str]) -> list[str]:
    """Distinct disease names from stored evidence_scores for these genes.

    Reuses the app's own curated disease→gene→food data to name the conditions
    a plant's active genes are already associated with. Best-effort: an empty
    or unavailable table simply yields no conditions.
    """
    if not gene_symbols:
        return []
    try:
        client = await get_supabase()
        resp = (
            await client.table("evidence_scores")
            .select("disease_name")
            .in_("gene_symbol", gene_symbols)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("CamScan conditions lookup failed: %s", exc)
        return []

    seen: set[str] = set()
    conditions: list[str] = []
    for row in resp.data or []:
        name = (row.get("disease_name") or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            conditions.append(name)
    return conditions


def _index_pathways(
    pathways: list[GenePathwayMapping],
) -> dict[str, list[GenePathwayMapping]]:
    by_gene: dict[str, list[GenePathwayMapping]] = {}
    for p in pathways:
        by_gene.setdefault(p.gene_symbol, []).append(p)
    return by_gene


def _grade(count: int) -> str:
    if count >= _GRADE_A_MIN:
        return "A"
    if count >= _GRADE_B_MIN:
        return "B"
    if count >= _GRADE_C_MIN:
        return "C"
    return "None"
