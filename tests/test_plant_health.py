"""Tests for the Plant.id / crop.kindwise health-assessment parsing.

The configured endpoint is crop.kindwise.com while the documented `health=all`
parameter is Plant.id v3, and no API key was available to confirm which shape
comes back. The parser therefore accepts both, and these tests pin that — plus
the rule that matters most: a missing or malformed assessment must degrade to
"unavailable" and never cost us the identification.
"""
from __future__ import annotations

from app.api.plantid_api import _normalize_health, _normalize_response


def _plant_result(**extra: object) -> dict:
    return {
        "result": {
            "is_plant": {"binary": True, "probability": 0.99},
            "classification": {
                "suggestions": [
                    {
                        "name": "Ocimum basilicum",
                        "probability": 0.93,
                        "details": {"common_names": ["Basil"]},
                        "similar_images": [{"url_small": "https://img/x.jpg"}],
                    }
                ]
            },
            **extra,
        }
    }


PLANT_ID_V3_DISEASE = {
    "is_healthy": {"binary": False, "probability": 0.12},
    "disease": {
        "suggestions": [
            {
                "name": "Powdery mildew",
                "probability": 0.85,
                "is_harmful": True,
                "details": {
                    "description": "A fungal disease causing white coating.",
                    "url": "https://example.org/powdery-mildew",
                    "treatment": {
                        "biological": ["Apply neem oil.", "Remove affected leaves."],
                        "chemical": "Apply a sulfur fungicide.",
                        "prevention": ["Ensure air circulation."],
                    },
                },
            },
            {"name": "Trace noise", "probability": 0.02, "is_harmful": True},
        ]
    },
}


class TestHealthParsing:
    def test_parses_plant_id_v3_shape(self) -> None:
        health = _normalize_health(_plant_result(**PLANT_ID_V3_DISEASE)["result"])

        assert health["health_assessment_available"] is True
        assert health["is_healthy"] is False
        assert health["health_probability"] == 0.12
        assert len(health["plant_conditions"]) == 1  # 2% suggestion dropped

        condition = health["plant_conditions"][0]
        assert condition["name"] == "Powdery mildew"
        assert condition["probability"] == 0.85
        assert condition["is_harmful"] is True
        assert condition["url"].startswith("https://")

    def test_treatment_lists_are_flattened_to_text(self) -> None:
        health = _normalize_health(_plant_result(**PLANT_ID_V3_DISEASE)["result"])
        treatment = health["plant_conditions"][0]["treatment"]

        # Arrives as a list from one provider and a string from the other.
        assert treatment["biological"] == "Apply neem oil. Remove affected leaves."
        assert treatment["chemical"] == "Apply a sulfur fungicide."
        assert treatment["prevention"] == "Ensure air circulation."

    def test_parses_alternative_health_assessment_key(self) -> None:
        result = _plant_result(
            health_assessment={
                "diseases": [
                    {"name": "Leaf spot", "probability": 0.6, "is_harmful": True}
                ]
            }
        )["result"]

        health = _normalize_health(result)

        assert health["health_assessment_available"] is True
        assert health["plant_conditions"][0]["name"] == "Leaf spot"

    def test_absent_assessment_degrades_quietly(self) -> None:
        health = _normalize_health(_plant_result()["result"])

        assert health["health_assessment_available"] is False
        assert health["plant_conditions"] == []
        # Nothing was assessed, so nothing is claimed to be wrong.
        assert health["is_healthy"] is True

    def test_healthy_plant_reports_no_conditions(self) -> None:
        result = _plant_result(
            is_healthy={"binary": True, "probability": 0.97}, disease={"suggestions": []}
        )["result"]

        health = _normalize_health(result)

        assert health["is_healthy"] is True
        assert health["health_probability"] == 0.97
        assert health["plant_conditions"] == []
        # The flag was present, so an assessment did happen.
        assert health["health_assessment_available"] is True

    def test_unlabelled_condition_is_treated_as_harmful(self) -> None:
        result = _plant_result(
            disease={"suggestions": [{"name": "Unknown blight", "probability": 0.5}]}
        )["result"]

        condition = _normalize_health(result)["plant_conditions"][0]

        assert condition["is_harmful"] is True

    def test_non_harmful_condition_keeps_its_flag(self) -> None:
        result = _plant_result(
            disease={
                "suggestions": [
                    {"name": "Lichen cover", "probability": 0.7, "is_harmful": False}
                ]
            }
        )["result"]
        health = _normalize_health(result)

        assert health["plant_conditions"][0]["is_harmful"] is False
        # No harmful finding and no explicit flag → inferred healthy.
        assert health["is_healthy"] is True

    def test_malformed_payload_does_not_raise(self) -> None:
        for junk in ({"disease": "nonsense"}, {"disease": {"suggestions": "nope"}},
                     {"is_healthy": "yes"}, {}):
            health = _normalize_health(junk)  # type: ignore[arg-type]
            assert isinstance(health["plant_conditions"], list)

    def test_probabilities_that_are_strings_are_coerced(self) -> None:
        result = _plant_result(
            disease={"suggestions": [{"name": "Rust", "probability": "0.75"}]}
        )["result"]

        assert _normalize_health(result)["plant_conditions"][0]["probability"] == 0.75


class TestIdentificationStillWorks:
    def test_health_fields_are_added_to_a_normal_identification(self) -> None:
        normalized = _normalize_response(_plant_result(**PLANT_ID_V3_DISEASE))

        # The pre-existing contract is untouched.
        assert normalized["plant_name"] == "Basil"
        assert normalized["scientific_name"] == "Ocimum basilicum"
        assert normalized["confidence"] == 0.93
        assert normalized["is_plant"] is True
        assert normalized["plant_image_url"] == "https://img/x.jpg"
        # And the new fields ride alongside.
        assert normalized["is_healthy"] is False
        assert normalized["health_assessment_available"] is True

    def test_non_plant_image_is_unaffected(self) -> None:
        normalized = _normalize_response({"result": {"is_plant": {"binary": False}}})

        assert normalized == {"is_plant": False}
