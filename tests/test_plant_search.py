"""Tests for POST /api/v1/plant/search and GET /api/v1/plant/suggestions.

Supabase, the CTD snapshot and Open Targets are all stubbed. Open Targets is
stubbed at the HTTP layer (httpx.MockTransport) so the real GraphQL parsing,
measurement filtering and failure handling in app/api/target_diseases.py run.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import ctd_api, target_diseases
from app.core import plant_lookup
from app.main import create_app

URL = "/api/v1/plant/search"
SUGGEST_URL = "/api/v1/plant/suggestions"

SOURCES = [
    {"phytochemical_name": "Curcumin", "fruit_vegetables": ["Turmeric", "Mango ginger"]},
    {"phytochemical_name": "Gingerol", "fruit_vegetables": ["Ginger"]},
    {"phytochemical_name": "Quercetin", "fruit_vegetables": ["Apple", "Onion", "Turmeric"]},
    {"phytochemical_name": "Phloretin", "fruit_vegetables": ["Custard apple"]},
    {"phytochemical_name": "Lutein", "fruit_vegetables": ["Turnip greens", "Kale"]},
]


def _pmids(n: int, start: int = 1000) -> list[str]:
    return [str(start + i) for i in range(n)]


CTD_ROWS = [
    # TNF: 12 PMIDs (grade A alone), PTGS2: 4 (B), NFKB1: 1 (C).
    {"chemical_name": "Curcumin", "gene_symbol": "TNF", "interaction_type": "decreases expression", "pmids": _pmids(12)},
    {"chemical_name": "Quercetin", "gene_symbol": "PTGS2", "interaction_type": "decreases activity", "pmids": _pmids(4, 5000)},
    {"chemical_name": "Curcumin", "gene_symbol": "NFKB1", "interaction_type": "affects binding", "pmids": ["9999"]},
]

ENSEMBL = {"TNF": "ENSG_TNF", "PTGS2": "ENSG_PTGS2", "NFKB1": "ENSG_NFKB1"}

# ensembl id -> [(disease id, name, score, therapeutic area ids)]
TARGET_DISEASES: dict[str, list[tuple[str, str, float, list[str]]]] = {
    "ENSG_TNF": [
        ("MONDO_0008383", "rheumatoid arthritis", 0.64, []),
        ("EFO_0004340", "body mass index", 0.5, ["EFO_0001444"]),  # measurement: dropped
        ("HP_0012603", "Abnormal urine sodium concentration", 0.6, []),  # phenotype: dropped
        ("MONDO_LOW", "weakly linked", 0.1, []),                   # below min score
    ],
    "ENSG_PTGS2": [
        ("MONDO_0008383", "rheumatoid arthritis", 0.5, []),
        ("EFO_0000685", "colorectal cancer", 0.45, []),
    ],
    "ENSG_NFKB1": [("MONDO_0005148", "type 2 diabetes mellitus", 0.4, [])],
}


def _open_targets_handler(
    target_diseases_map: dict[str, list[tuple[str, str, float, list[str]]]] | None = None,
    fail_targets: set[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    disease_map = TARGET_DISEASES if target_diseases_map is None else target_diseases_map

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        variables = body["variables"]
        if "mapIds" in body["query"]:
            mappings = [
                {"term": t, "hits": [{"id": ENSEMBL[t], "name": t, "entity": "target"}] if t in ENSEMBL else []}
                for t in variables["terms"]
            ]
            return httpx.Response(200, json={"data": {"mapIds": {"mappings": mappings}}})
        ensembl_id = variables["ensemblId"]
        if fail_targets and ensembl_id in fail_targets:
            return httpx.Response(503, text="unavailable")
        rows = [
            {"score": score, "disease": {"id": did, "name": name, "therapeuticAreas": [{"id": a} for a in areas]}}
            for did, name, score, areas in disease_map.get(ensembl_id, [])
        ]
        return httpx.Response(
            200, json={"data": {"target": {"approvedSymbol": "", "associatedDiseases": {"rows": rows}}}}
        )

    return handler


def _install_open_targets(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
) -> None:
    monkeypatch.setattr(
        target_diseases,
        "make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


class _FakeQuery:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def select(self, *_args: Any, **_kwargs: Any) -> "_FakeQuery":
        return self

    async def execute(self) -> Any:
        return type("Resp", (), {"data": self._rows})()


class _FakeSupabase:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.reads = 0

    def table(self, name: str) -> _FakeQuery:
        assert name == "phytochemical_sources"
        self.reads += 1
        return _FakeQuery(self.rows)


@pytest.fixture
def supabase(monkeypatch: pytest.MonkeyPatch) -> _FakeSupabase:
    fake = _FakeSupabase(SOURCES)

    async def get_fake() -> _FakeSupabase:
        return fake

    monkeypatch.setattr(plant_lookup, "get_supabase", get_fake)
    return fake


@pytest.fixture(autouse=True)
def _stubs(monkeypatch: pytest.MonkeyPatch, supabase: _FakeSupabase) -> None:
    plant_lookup.clear_sources_cache()

    async def fake_ctd(chemicals: list[str]) -> list[dict[str, Any]]:
        wanted = {c.lower() for c in chemicals}
        return [r for r in CTD_ROWS if r["chemical_name"].lower() in wanted]

    monkeypatch.setattr(ctd_api, "get_genes_for_chemicals", fake_ctd)
    _install_open_targets(monkeypatch, _open_targets_handler())
    yield
    plant_lookup.clear_sources_cache()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


# ── Search ────────────────────────────────────────────────────────────────────
def test_happy_path_shape(client: TestClient) -> None:
    resp = client.post(URL, json={"plant_name": "Turmeric"})

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "plant_name", "scientific_name", "phytochemicals", "nutrient_content",
        "conditions_with_evidence", "total_conditions", "total_publications",
        "recommendation_source",
    }
    assert body["plant_name"] == "Turmeric"
    # The table has neither column; nothing is invented for them.
    assert body["scientific_name"] is None
    assert body["nutrient_content"] is None
    assert body["phytochemicals"] == ["Curcumin", "Quercetin"]
    assert body["recommendation_source"] == "academic_literature"

    conditions = body["conditions_with_evidence"]
    names = [c["condition_name"] for c in conditions]
    assert names == ["rheumatoid arthritis", "colorectal cancer", "type 2 diabetes mellitus"]
    assert "body mass index" not in names and "weakly linked" not in names
    assert "Abnormal urine sodium concentration" not in names
    assert body["total_conditions"] == 3

    ra = conditions[0]
    assert ra["condition_id"] == "MONDO_0008383"
    assert ra["gene_targets"] == ["TNF", "PTGS2"]  # strongest gene first
    assert ra["publication_count"] == 16           # union of TNF (12) + PTGS2 (4)
    assert ra["evidence_grade"] == "A"
    assert ra["interaction_type"] == "decreases expression"
    assert ra["sample_citations"] == ["PMID 1000", "PMID 1001", "PMID 1002"]

    assert conditions[1]["evidence_grade"] == "B"
    assert conditions[2]["evidence_grade"] == "C"
    assert body["total_publications"] == 17


def test_match_is_case_insensitive_and_returns_canonical_name(client: TestClient) -> None:
    resp = client.post(URL, json={"plant_name": "  tURMERIC "})

    assert resp.status_code == 200
    assert resp.json()["plant_name"] == "Turmeric"


def test_exact_name_beats_word_level_match(client: TestClient) -> None:
    # "Custard apple" shares a word with "Apple" but is a different plant.
    body = client.post(URL, json={"plant_name": "apple"}).json()

    assert body["plant_name"] == "Apple"
    assert body["phytochemicals"] == ["Quercetin"]


def test_word_level_fallback_when_no_exact_match(client: TestClient) -> None:
    body = client.post(URL, json={"plant_name": "Red Onion"}).json()

    assert body["plant_name"] == "Onion"
    assert body["phytochemicals"] == ["Quercetin"]


def test_ambiguous_word_is_404_not_a_union_of_plants(
    client: TestClient, supabase: _FakeSupabase
) -> None:
    supabase.rows = [
        *SOURCES,
        {"phytochemical_name": "Anthocyanin", "fruit_vegetables": ["Red Cabbage"]},
        {"phytochemical_name": "Theaflavin", "fruit_vegetables": ["Red tea"]},
    ]

    resp = client.post(URL, json={"plant_name": "Red"})

    assert resp.status_code == 404


def test_fallback_collects_only_the_single_resolved_plant(
    client: TestClient, supabase: _FakeSupabase
) -> None:
    supabase.rows = [
        *SOURCES,
        {"phytochemical_name": "Anthocyanin", "fruit_vegetables": ["Red Cabbage"]},
        {"phytochemical_name": "Sinigrin", "fruit_vegetables": ["Savoy cabbage leaves"]},
    ]

    # "Red Cabbage" resolves exactly, never pooling with other cabbages.
    body = client.post(URL, json={"plant_name": "red cabbage"}).json()
    assert body["plant_name"] == "Red Cabbage"
    assert body["phytochemicals"] == ["Anthocyanin"]

    # "Cabbage" alone is ambiguous between the two.
    assert client.post(URL, json={"plant_name": "Cabbage"}).status_code == 404


def test_unknown_plant_is_404(client: TestClient) -> None:
    resp = client.post(URL, json={"plant_name": "Dragonfruit"})

    assert resp.status_code == 404
    assert "Dragonfruit" in resp.json()["detail"]


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_empty_or_oversized_name_is_422(client: TestClient, name: str) -> None:
    assert client.post(URL, json={"plant_name": name}).status_code == 422


def test_open_targets_outage_degrades_to_no_conditions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("open targets unreachable")

    _install_open_targets(monkeypatch, down)

    resp = client.post(URL, json={"plant_name": "Turmeric"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["phytochemicals"] == ["Curcumin", "Quercetin"]
    assert body["conditions_with_evidence"] == []
    assert body["total_conditions"] == 0
    assert body["total_publications"] == 0


def test_single_gene_failure_is_skipped(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_open_targets(monkeypatch, _open_targets_handler(fail_targets={"ENSG_TNF"}))

    body = client.post(URL, json={"plant_name": "Turmeric"}).json()

    by_name = {c["condition_name"]: c for c in body["conditions_with_evidence"]}
    # Still linked through PTGS2, but now graded on PTGS2's PMIDs alone.
    assert by_name["rheumatoid arthritis"]["gene_targets"] == ["PTGS2"]
    assert by_name["rheumatoid arthritis"]["evidence_grade"] == "B"
    assert "type 2 diabetes mellitus" in by_name


def test_slow_open_targets_is_cut_off_at_overall_budget(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast = _open_targets_handler()

    async def slow_tnf(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["variables"].get("ensemblId") == "ENSG_TNF":
            await asyncio.sleep(30)
        return fast(request)

    _install_open_targets(monkeypatch, slow_tnf)
    monkeypatch.setattr(target_diseases, "_TOTAL_BUDGET_SECONDS", 0.5)

    started = time.monotonic()
    resp = client.post(URL, json={"plant_name": "Turmeric"})

    assert time.monotonic() - started < 5
    assert resp.status_code == 200
    by_name = {c["condition_name"]: c for c in resp.json()["conditions_with_evidence"]}
    # TNF timed out and was dropped; the genes that answered still count.
    assert by_name["rheumatoid arthritis"]["gene_targets"] == ["PTGS2"]
    assert "type 2 diabetes mellitus" in by_name


def test_no_ctd_interactions_returns_empty_conditions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def none(_chemicals: list[str]) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(ctd_api, "get_genes_for_chemicals", none)

    body = client.post(URL, json={"plant_name": "Ginger"}).json()

    assert body["phytochemicals"] == ["Gingerol"]
    assert body["conditions_with_evidence"] == []


def test_top_ten_cap_and_ordering(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Every gene links to its own condition; counts span all three grades.
    counts = [1, 15, 2, 3, 10, 4, 1, 9, 2, 12, 5, 1, 3]
    rows = [
        {"chemical_name": "Curcumin", "gene_symbol": f"G{i}", "interaction_type": "", "pmids": _pmids(n, 10_000 * (i + 1))}
        for i, n in enumerate(counts)
    ]

    async def fake_ctd(_chemicals: list[str]) -> list[dict[str, Any]]:
        return rows

    monkeypatch.setattr(ctd_api, "get_genes_for_chemicals", fake_ctd)
    ENSEMBL.update({f"G{i}": f"ENSG_G{i}" for i in range(len(counts))})
    try:
        diseases = {f"ENSG_G{i}": [(f"EFO_{i}", f"condition {i}", 0.5, [])] for i in range(len(counts))}
        _install_open_targets(monkeypatch, _open_targets_handler(diseases))

        body = client.post(URL, json={"plant_name": "Turmeric"}).json()
    finally:
        for i in range(len(counts)):
            ENSEMBL.pop(f"G{i}", None)

    conditions = body["conditions_with_evidence"]
    assert len(conditions) == 10
    assert body["total_conditions"] == len(counts)
    pubs = [c["publication_count"] for c in conditions]
    assert pubs == sorted(counts, reverse=True)[:10]
    grades = [c["evidence_grade"] for c in conditions]
    assert grades == sorted(grades)  # "A" < "B" < "C": strongest first


@pytest.mark.parametrize(
    ("count", "grade"),
    [(0, "None"), (1, "C"), (2, "C"), (3, "B"), (9, "B"), (10, "A"), (250, "A")],
)
def test_grading_thresholds_match_chain_builder(count: int, grade: str) -> None:
    assert plant_lookup.grade_from_pmid_count(count) == grade


# ── Suggestions ───────────────────────────────────────────────────────────────
def test_suggestions_match_case_insensitively_prefix_first(client: TestClient) -> None:
    resp = client.get(SUGGEST_URL, params={"q": "TUR"})

    assert resp.status_code == 200
    assert resp.json() == ["Turmeric", "Turnip greens"]


def test_suggestions_include_substring_matches_after_prefix(client: TestClient) -> None:
    assert client.get(SUGGEST_URL, params={"q": "app"}).json() == ["Apple", "Custard apple"]


@pytest.mark.parametrize("q", ["", "t", " t "])
def test_short_query_returns_empty(client: TestClient, q: str) -> None:
    resp = client.get(SUGGEST_URL, params={"q": q})

    assert resp.status_code == 200
    assert resp.json() == []


def test_overlong_query_returns_empty_array_not_422(client: TestClient) -> None:
    resp = client.get(SUGGEST_URL, params={"q": "turmeric " * 20})

    assert resp.status_code == 200
    assert resp.json() == []


def test_suggestions_are_capped(client: TestClient, supabase: _FakeSupabase) -> None:
    supabase.rows = [{"phytochemical_name": "X", "fruit_vegetables": [f"Berry {i:02d}" for i in range(25)]}]

    assert len(client.get(SUGGEST_URL, params={"q": "berry"}).json()) == 10


def test_plant_list_is_cached_between_requests(client: TestClient, supabase: _FakeSupabase) -> None:
    client.get(SUGGEST_URL, params={"q": "tur"})
    client.get(SUGGEST_URL, params={"q": "gin"})
    client.post(URL, json={"plant_name": "Ginger"})

    assert supabase.reads == 1
