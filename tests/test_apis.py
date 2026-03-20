"""Integration and unit tests for the automation engine."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import AsyncClient

from app.main import app
from app.models import (
    CompoundGeneInteraction,
    DiseaseGeneAssociation,
    EvidenceLevel,
    FoodCompoundMapping,
    GenePathwayMapping,
)
from app.core.evidence_scoring import EvidenceChain, compute_evidence_score


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def sync_client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def sample_gene() -> DiseaseGeneAssociation:
    return DiseaseGeneAssociation(
        disease_id="C0011849",
        disease_name="Diabetes Mellitus, Type 2",
        gene_id="3643",
        gene_symbol="INSR",
        score=0.85,
    )


@pytest.fixture
def sample_pathway(sample_gene: DiseaseGeneAssociation) -> GenePathwayMapping:
    return GenePathwayMapping(
        gene_id=sample_gene.gene_id,
        gene_symbol=sample_gene.gene_symbol,
        pathway_id="R-HSA-74752",
        pathway_name="Signaling by Insulin receptor",
    )


@pytest.fixture
def sample_compound(sample_gene: DiseaseGeneAssociation) -> CompoundGeneInteraction:
    return CompoundGeneInteraction(
        compound_id="CHEMBL641",
        compound_name="Quercetin",
        gene_id=sample_gene.gene_id,
        gene_symbol=sample_gene.gene_symbol,
        activity_type="IC50",
        activity_value=500.0,
        activity_units="nM",
    )


@pytest.fixture
def sample_food() -> FoodCompoundMapping:
    return FoodCompoundMapping(
        food_id="170383",
        food_name="Apples, raw, with skin",
        compound_name="Quercetin",
        compound_amount=4.42,
        compound_unit="mg",
    )


# ── Health check ──────────────────────────────────────────────────────────────

def test_health_check(sync_client: TestClient) -> None:
    response = sync_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_root(sync_client: TestClient) -> None:
    response = sync_client.get("/")
    assert response.status_code == 200
    assert "NatureWellness" in response.json()["message"]


# ── Evidence scoring unit tests ───────────────────────────────────────────────

def test_compute_evidence_score_high(
    sample_gene: DiseaseGeneAssociation,
    sample_pathway: GenePathwayMapping,
    sample_compound: CompoundGeneInteraction,
    sample_food: FoodCompoundMapping,
) -> None:
    chain = EvidenceChain(
        disease_gene=sample_gene,
        pathway=sample_pathway,
        compound=sample_compound,
        food=sample_food,
    )
    score = compute_evidence_score(chain)

    assert 0 <= score.score <= 100
    assert score.evidence_level in list(EvidenceLevel)
    assert score.disease_id == sample_gene.disease_id
    assert score.food_id == sample_food.food_id
    assert score.reasoning is not None


def test_compute_evidence_score_no_activity(
    sample_gene: DiseaseGeneAssociation,
    sample_pathway: GenePathwayMapping,
    sample_food: FoodCompoundMapping,
) -> None:
    compound = CompoundGeneInteraction(
        compound_id="CHEMBL999",
        compound_name="Curcumin",
        gene_id=sample_gene.gene_id,
        gene_symbol=sample_gene.gene_symbol,
        activity_value=None,
        activity_units=None,
    )
    chain = EvidenceChain(
        disease_gene=sample_gene,
        pathway=sample_pathway,
        compound=compound,
        food=sample_food,
    )
    score = compute_evidence_score(chain)
    assert score.score > 0


def test_low_gene_score_yields_lower_evidence(
    sample_pathway: GenePathwayMapping,
    sample_compound: CompoundGeneInteraction,
    sample_food: FoodCompoundMapping,
) -> None:
    weak_gene = DiseaseGeneAssociation(
        disease_id="C0011849",
        disease_name="Diabetes Mellitus, Type 2",
        gene_id="3643",
        gene_symbol="INSR",
        score=0.05,
    )
    strong_gene = DiseaseGeneAssociation(
        disease_id="C0011849",
        disease_name="Diabetes Mellitus, Type 2",
        gene_id="3643",
        gene_symbol="INSR",
        score=0.95,
    )
    weak_score = compute_evidence_score(
        EvidenceChain(weak_gene, sample_pathway, sample_compound, sample_food)
    )
    strong_score = compute_evidence_score(
        EvidenceChain(strong_gene, sample_pathway, sample_compound, sample_food)
    )
    assert strong_score.score > weak_score.score


# ── Automation endpoint (mocked pipeline) ────────────────────────────────────

@pytest.mark.asyncio
async def test_run_automation_endpoint_mocked() -> None:
    from app.models import AutomationRunResponse, EvidenceScore

    mock_response = AutomationRunResponse(
        run_id="test-run-123",
        disease_id="C0011849",
        disease_name="Diabetes Mellitus, Type 2",
        genes_found=3,
        pathways_found=5,
        compounds_found=8,
        foods_found=12,
        evidence_scores=[],
        status="completed",
    )

    with patch("app.routers.automation.run_pipeline", new=AsyncMock(return_value=mock_response)):
        async with AsyncClient(app=app, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/automation/run",
                json={
                    "disease_id": "C0011849",
                    "disease_name": "Diabetes Mellitus, Type 2",
                    "max_genes": 10,
                    "min_gene_score": 0.3,
                },
            )

    assert response.status_code == 200
    data = response.json()
    assert data["run_id"] == "test-run-123"
    assert data["genes_found"] == 3


# ── Manual review endpoint tests ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_review_mocked() -> None:
    from app.models import ReviewRecord, ManualReviewStatus

    mock_row = {
        "id": "rev-001",
        "evidence_score_id": "score-abc",
        "status": ManualReviewStatus.pending.value,
        "reviewer_notes": None,
        "reviewed_by": None,
        "reviewed_at": None,
        "created_at": None,
    }

    with patch("app.routers.manual_review.insert_record", new=AsyncMock(return_value=mock_row)):
        async with AsyncClient(app=app, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/review/",
                params={"evidence_score_id": "score-abc"},
            )

    assert response.status_code == 201
    assert response.json()["status"] == "pending"
