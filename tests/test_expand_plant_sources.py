"""Tests for the pure planning logic in scripts/expand_plant_sources.py."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "expand_plant_sources.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("expand_plant_sources", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


eps = _load()


@pytest.fixture
def plants(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    mapping = {
        "Zobo": ["Quercetin", "Anthocyanins", "Hibiscus acid"],
        "Avocado": ["quercetin", "Zeaxanthin"],
    }
    monkeypatch.setattr(eps, "PLANTS", mapping)
    return mapping


def test_resolves_case_insensitively_and_via_aliases(plants: dict[str, list[str]]) -> None:
    resolved, unresolved = eps.resolve_chemicals(["Quercetin", "Anthocyanins", "Zeaxanthins"])

    assert resolved == {
        "Quercetin": ["Zobo", "Avocado"],
        "Anthocyanins": ["Zobo"],
        "Zeaxanthins": ["Avocado"],
    }
    assert unresolved == {"Hibiscus acid": ["Zobo"]}


def test_existing_rows_keep_their_plants_and_spelling() -> None:
    existing = [{"phytochemical_name": "quercetin", "fruit_vegetables": ["Onion", "zobo"]}]

    updates, inserts = eps.plan_upserts({"Quercetin": ["Zobo", "Avocado"]}, existing)

    assert updates == [
        {"phytochemical_name": "quercetin", "fruit_vegetables": ["Onion", "zobo", "Avocado"]}
    ]
    assert inserts == []


def test_rows_with_nothing_new_are_not_rewritten() -> None:
    existing = [{"phytochemical_name": "Quercetin", "fruit_vegetables": ["Zobo"]}]

    assert eps.plan_upserts({"Quercetin": ["ZOBO"]}, existing) == ([], [])


def test_new_compounds_become_new_rows() -> None:
    updates, inserts = eps.plan_upserts({"Zeaxanthins": ["Avocado", "Corn", "Kale", "Spinach"]}, [])

    assert updates == []
    assert inserts == [
        {
            "phytochemical_name": "Zeaxanthins",
            "fruit_vegetables": ["Avocado", "Corn", "Kale", "Spinach"],
            "primary_sources": ["Avocado", "Corn", "Kale"],
            "chemical_class": None,
        }
    ]


def test_requested_plants_are_all_mapped() -> None:
    required = {
        "Banana", "Mango", "Moringa", "Neem", "Dogonyaro", "Bitter leaf", "Scent leaf", "Uziza",
        "Utazi", "Agbalumo", "Tiger nut", "Baobab", "Zobo", "Kola nut", "Uda", "Ehuru", "Ogbono",
        "Turmeric", "Green tea", "Rooibos", "Ugwu", "Pawpaw",
    }
    assert required <= set(eps.PLANTS)
    assert all(eps.PLANTS[p] for p in required)
