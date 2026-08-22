"""Tests for POST /api/v1/summary.

The Anthropic call itself is stubbed — these cover everything this codebase is
responsible for: the request contract, response parsing, caching, and the
mapping of SDK failures onto HTTP status codes.
"""
from __future__ import annotations

import types
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.routers import summary as summary_module


class _Block:
    """Stand-in for an SDK content block."""

    def __init__(self, text: str, block_type: str = "text") -> None:
        self.type = block_type
        self.text = text


class _Message:
    def __init__(self, blocks: list[_Block], stop_reason: str = "end_turn") -> None:
        self.content = blocks
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeClient:
    last: "_FakeClient | None" = None

    def __init__(self, result: Any) -> None:
        self.messages = _FakeMessages(result)
        _FakeClient.last = self


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a configured key, an empty cache and a fresh settings read."""
    summary_module._CACHE.clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    summary_module._CACHE.clear()


def _install_sdk(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    """Inject a stub `anthropic` module for the router's late import."""
    module = types.ModuleType("anthropic")
    module.AsyncAnthropic = lambda **_kwargs: _FakeClient(result)  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "anthropic", module)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


PAYLOAD = {
    "disease_name": "Type 2 Diabetes",
    "foods": ["Grape", "Turmeric", "Blueberry"],
    "phytochemicals": ["Resveratrol", "Curcumin"],
    "genes": ["PPARG", "TNF"],
    "evidence_grade": "Grade A",
}


def test_returns_summary_text(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    _install_sdk(monkeypatch, _Message([_Block("Evidence suggests these foods may support.")]))

    response = client.post("/api/v1/summary", json=PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == "Evidence suggests these foods may support."
    assert body["cached"] is False
    assert body["model"]


def test_accepts_both_field_spellings(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    _install_sdk(monkeypatch, _Message([_Block("ok")]))

    short = client.post("/api/v1/summary", json=PAYLOAD)
    prompt_short = _FakeClient.last.messages.calls[0]["messages"][0]["content"]

    summary_module._CACHE.clear()
    long_form = client.post(
        "/api/v1/summary",
        json={
            "disease_name": PAYLOAD["disease_name"],
            "top_foods": PAYLOAD["foods"],
            "top_phytochemicals": PAYLOAD["phytochemicals"],
            "top_genes": PAYLOAD["genes"],
            "evidence_grade": PAYLOAD["evidence_grade"],
        },
    )
    prompt_long = _FakeClient.last.messages.calls[0]["messages"][0]["content"]

    assert short.status_code == long_form.status_code == 200
    # Identical inputs under either spelling must produce an identical prompt.
    assert prompt_short == prompt_long
    assert "Grape" in prompt_short and "Resveratrol" in prompt_short


def test_prompt_carries_the_guardrails(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    _install_sdk(monkeypatch, _Message([_Block("ok")]))
    client.post("/api/v1/summary", json=PAYLOAD)

    call = _FakeClient.last.messages.calls[0]
    assert "never claim cure or treatment" in call["messages"][0]["content"].lower()
    assert "cures, treats or prevents" in call["system"].lower()
    assert call["max_tokens"] <= 300


def test_second_identical_request_is_served_from_cache(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    _install_sdk(monkeypatch, _Message([_Block("cached text")]))

    first = client.post("/api/v1/summary", json=PAYLOAD)
    call_count_after_first = len(_FakeClient.last.messages.calls)
    second = client.post("/api/v1/summary", json=PAYLOAD)

    assert first.json()["cached"] is False
    assert second.json()["summary"] == "cached text"
    assert second.json()["cached"] is True
    # The cache hit must not reach the SDK again.
    assert call_count_after_first == 1


def test_missing_api_key_is_503(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "", raising=False)

    response = client.post("/api/v1/summary", json=PAYLOAD)

    assert response.status_code == 503


def test_refusal_is_502(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    _install_sdk(monkeypatch, _Message([_Block("")], stop_reason="refusal"))

    assert client.post("/api/v1/summary", json=PAYLOAD).status_code == 502


def test_sdk_failure_is_502(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    _install_sdk(monkeypatch, RuntimeError("upstream exploded"))

    response = client.post("/api/v1/summary", json=PAYLOAD)

    assert response.status_code == 502
    # The upstream message must not leak to the client.
    assert "exploded" not in response.text


def test_empty_response_is_502(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    _install_sdk(monkeypatch, _Message([_Block("", "thinking")]))

    assert client.post("/api/v1/summary", json=PAYLOAD).status_code == 502


def test_disease_name_is_required(client: TestClient) -> None:
    assert client.post("/api/v1/summary", json={"foods": ["Grape"]}).status_code == 422


def test_oversized_lists_are_rejected(client: TestClient) -> None:
    payload = dict(PAYLOAD, foods=[f"food-{i}" for i in range(50)])

    assert client.post("/api/v1/summary", json=payload).status_code == 422
