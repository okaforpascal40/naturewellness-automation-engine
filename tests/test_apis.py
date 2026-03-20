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
        disease_id="EFO_0001360",
        disease_name="type 2 diabetes mellitus",
        gene_id="ENSG00000171105",
        gene_symbol="INSR",
        score=0.85,
        source="open_targets",
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
        disease_id="EFO_0001360",
        disease_name="type 2 diabetes mellitus",
        gene_id="ENSG00000171105",
        gene_symbol="INSR",
        score=0.05,
        source="open_targets",
    )
    strong_gene = DiseaseGeneAssociation(
        disease_id="EFO_0001360",
        disease_name="type 2 diabetes mellitus",
        gene_id="ENSG00000171105",
        gene_symbol="INSR",
        score=0.95,
        source="open_targets",
    )
    weak_score = compute_evidence_score(
        EvidenceChain(weak_gene, sample_pathway, sample_compound, sample_food)
    )
    strong_score = compute_evidence_score(
        EvidenceChain(strong_gene, sample_pathway, sample_compound, sample_food)
    )
    assert strong_score.score > weak_score.score


# ── Open Targets API unit tests ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_open_targets_returns_associations() -> None:
    """get_disease_gene_associations parses a well-formed Open Targets response."""
    from app.api.disgenet import get_disease_gene_associations

    mock_graphql_response = {
        "data": {
            "disease": {
                "id": "EFO_0001360",
                "name": "type 2 diabetes mellitus",
                "associatedTargets": {
                    "count": 2,
                    "rows": [
                        {
                            "target": {
                                "id": "ENSG00000171105",
                                "approvedSymbol": "INSR",
                                "approvedName": "insulin receptor",
                            },
                            "score": 0.92,
                        },
                        {
                            "target": {
                                "id": "ENSG00000254647",
                                "approvedSymbol": "INS",
                                "approvedName": "insulin",
                            },
                            "score": 0.87,
                        },
                    ],
                },
            }
        }
    }

    import httpx
    from unittest.mock import AsyncMock, patch

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = mock_graphql_response

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
        results = await get_disease_gene_associations(
            disease_id="EFO_0001360",
            min_score=0.3,
            limit=10,
        )

    assert len(results) == 2
    assert results[0].gene_symbol == "INSR"
    assert results[0].score == 0.92
    assert results[0].source == "open_targets"
    assert results[0].disease_name == "type 2 diabetes mellitus"


@pytest.mark.asyncio
async def test_open_targets_filters_by_min_score() -> None:
    """Associations below min_score are excluded."""
    from app.api.disgenet import get_disease_gene_associations

    mock_graphql_response = {
        "data": {
            "disease": {
                "id": "EFO_0001360",
                "name": "type 2 diabetes mellitus",
                "associatedTargets": {
                    "count": 2,
                    "rows": [
                        {
                            "target": {"id": "ENSG00000171105", "approvedSymbol": "INSR", "approvedName": "insulin receptor"},
                            "score": 0.9,
                        },
                        {
                            "target": {"id": "ENSG00000999999", "approvedSymbol": "WEAK", "approvedName": "weak gene"},
                            "score": 0.1,
                        },
                    ],
                },
            }
        }
    }

    import httpx

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = mock_graphql_response

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
        results = await get_disease_gene_associations(
            disease_id="EFO_0001360",
            min_score=0.5,
            limit=10,
        )

    assert len(results) == 1
    assert results[0].gene_symbol == "INSR"


@pytest.mark.asyncio
async def test_open_targets_unknown_disease_returns_empty() -> None:
    """Missing disease node in response yields an empty list without raising."""
    from app.api.disgenet import get_disease_gene_associations

    import httpx

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"data": {"disease": None}}

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
        results = await get_disease_gene_associations("EFO_UNKNOWN", min_score=0.3, limit=10)

    assert results == []


# ── Automation endpoint (mocked pipeline) ────────────────────────────────────

@pytest.mark.asyncio
async def test_run_automation_endpoint_mocked() -> None:
    from app.models import AutomationRunResponse, EvidenceScore

    mock_response = AutomationRunResponse(
        run_id="test-run-123",
        disease_id="EFO_0001360",
        disease_name="type 2 diabetes mellitus",
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
                    "disease_id": "EFO_0001360",
                    "disease_name": "type 2 diabetes mellitus",
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
