from datetime import datetime
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class ManualReviewStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    needs_revision = "needs_revision"


class EvidenceLevel(str, Enum):
    high = "high"
    moderate = "moderate"
    low = "low"
    insufficient = "insufficient"


# ── Domain entities ──────────────────────────────────────────────────────────

class DiseaseGeneAssociation(BaseModel):
    id: str | None = None
    disease_id: str = Field(..., description="UMLS / MeSH disease identifier")
    disease_name: str
    gene_id: str = Field(..., description="NCBI Gene ID")
    gene_symbol: str
    score: float = Field(..., ge=0.0, le=1.0, description="DisGeNET association score")
    source: str = Field(default="disgenet")
    created_at: datetime | None = None


class GenePathwayMapping(BaseModel):
    id: str | None = None
    gene_id: str
    gene_symbol: str
    pathway_id: str = Field(..., description="Reactome stable identifier")
    pathway_name: str
    species: str = Field(default="Homo sapiens")
    source: str = Field(default="reactome")
    created_at: datetime | None = None


class CompoundGeneInteraction(BaseModel):
    id: str | None = None
    compound_id: str = Field(..., description="ChEMBL compound ID")
    compound_name: str
    gene_id: str
    gene_symbol: str
    activity_type: str | None = None
    activity_value: float | None = None
    activity_units: str | None = None
    source: str = Field(default="chembl")
    created_at: datetime | None = None


class FoodCompoundMapping(BaseModel):
    id: str | None = None
    food_id: str = Field(..., description="USDA FDC ID")
    food_name: str
    compound_name: str
    compound_amount: float | None = None
    compound_unit: str | None = None
    source: str = Field(default="usda")
    created_at: datetime | None = None


class EvidenceScore(BaseModel):
    id: str | None = None
    disease_id: str
    disease_name: str
    food_id: str
    food_name: str
    compound_name: str
    gene_symbol: str
    pathway_name: str
    score: float = Field(..., ge=0.0, le=100.0)
    evidence_level: EvidenceLevel
    reasoning: str | None = None
    created_at: datetime | None = None


class ReviewRecord(BaseModel):
    id: str | None = None
    evidence_score_id: str
    status: ManualReviewStatus = ManualReviewStatus.pending
    reviewer_notes: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime | None = None


# ── Request / Response schemas ────────────────────────────────────────────────

class AutomationRunRequest(BaseModel):
    disease_id: str
    disease_name: str = ""
    max_genes: int = Field(default=10, ge=1, le=50)
    min_gene_score: float = Field(default=0.3, ge=0.0, le=1.0)


class FruitRecommendation(BaseModel):
    fruit_vegetable: str
    phytochemical: str
    gene_target: str
    interaction_type: str = ""
    evidence_grade: str = "None"
    publication_count: int = 0
    sample_citations: list[str] = Field(default_factory=list)
    pathway: str = ""


class AutomationRunResponse(BaseModel):
    run_id: str
    disease_id: str
    disease_name: str
    genes_found: int
    pathways_found: int
    compounds_found: int
    foods_found: int
    evidence_scores: list[EvidenceScore] = Field(default_factory=list)
    recommendations: list[FruitRecommendation] = Field(default_factory=list)
    status: str


class ReviewUpdateRequest(BaseModel):
    status: ManualReviewStatus
    reviewer_notes: str | None = None
    reviewed_by: str | None = None


class PaginatedResponse(BaseModel):
    items: list[Any]
    total: int
    page: int
    page_size: int
    pages: int
