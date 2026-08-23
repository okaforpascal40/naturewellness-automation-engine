"""Tests for the infectious-disease recovery ladder.

Network calls are stubbed; these cover the routing decisions and the safety
properties of the curated data, which are the parts that can silently go wrong.
"""
from __future__ import annotations

import pytest

from app.api import disease_synonyms
from app.data import infectious_disease_compounds as idc


class TestCuratedMap:
    def test_lookup_matches_on_substring(self) -> None:
        hit = idc.lookup("Typhoid Fever")
        assert hit is not None
        key, entry = hit
        assert key == "typhoid"
        assert "TNF" in entry["genes"]

    def test_lookup_is_case_insensitive(self) -> None:
        assert idc.lookup("TUBERCULOSIS") is not None
        assert idc.lookup("Pulmonary tuberculosis") is not None

    def test_unknown_disease_returns_none(self) -> None:
        assert idc.lookup("osteoarthritis") is None
        assert idc.lookup("") is None

    @pytest.mark.parametrize("key", sorted(idc.INFECTIOUS_DISEASE_MAP))
    def test_gene_symbols_look_like_hgnc(self, key: str) -> None:
        """A symbol CTD cannot match yields nothing, silently.

        "NF-KB" is the trap: it reads as a real gene but the approved symbol is
        NFKB1, and the lookup would just come back empty.
        """
        for symbol in idc.INFECTIOUS_DISEASE_MAP[key]["genes"]:
            assert symbol == symbol.upper(), symbol
            assert symbol.replace("-", "").isalnum(), symbol
            assert "-" not in symbol, f"{symbol} is not an approved HGNC symbol"

    def test_malaria_does_not_suggest_artemisinin(self) -> None:
        """WHO advises against Artemisia annua preparations for malaria.

        Artemisinin is a frontline antimalarial; sub-therapeutic dietary dosing
        drives resistance, so it must never be surfaced as a food suggestion.
        """
        compounds = [c.lower() for c in idc.INFECTIOUS_DISEASE_MAP["malaria"]["typical_compounds"]]
        assert not any("artemisin" in c for c in compounds)
        assert not any("artemisia" in c for c in compounds)

    @pytest.mark.parametrize("key", sorted(idc.INFECTIOUS_DISEASE_MAP))
    def test_every_entry_carries_a_note(self, key: str) -> None:
        entry = idc.INFECTIOUS_DISEASE_MAP[key]
        assert entry["evidence_note"].strip()
        assert entry["mechanism"].strip()

    @pytest.mark.parametrize("key", sorted(idc.INFECTIOUS_DISEASE_MAP))
    def test_metadata_always_carries_the_treatment_disclaimer(self, key: str) -> None:
        """Every disease here can kill and every one has a real treatment."""
        meta = idc.response_metadata(idc.INFECTIOUS_DISEASE_MAP[key])
        assert meta["data_source"] == "curated_literature"
        assert "not a substitute for medical care" in meta["disclaimer"]
        assert meta["evidence_note"].strip()

    def test_no_treatment_claims_in_mechanisms(self) -> None:
        banned = ("cure", "cures", "treat", "treats", "treatment for", "prevents")
        for key, entry in idc.INFECTIOUS_DISEASE_MAP.items():
            text = f"{entry['mechanism']} {entry['evidence_note']}".lower()
            for word in banned:
                assert word not in text.split(), f"{key} claims '{word}': {text}"


class TestSynonyms:
    def test_known_disease_returns_alternatives(self) -> None:
        assert disease_synonyms.synonyms_for("Typhoid Fever")
        assert "salmonella infection" in disease_synonyms.synonyms_for("typhoid fever")

    def test_matching_is_substring_and_case_insensitive(self) -> None:
        assert disease_synonyms.synonyms_for("Severe COVID-19 pneumonia")

    def test_unknown_disease_returns_empty(self) -> None:
        assert disease_synonyms.synonyms_for("type 2 diabetes") == []
        assert disease_synonyms.synonyms_for("") == []

    def test_score_floors_descend_and_stop_above_noise(self) -> None:
        floors = disease_synonyms._FALLBACK_SCORE_FLOORS
        assert list(floors) == sorted(floors, reverse=True)
        # Below 0.05 Open Targets associations are single weak text-mining hits.
        assert min(floors) >= 0.05
