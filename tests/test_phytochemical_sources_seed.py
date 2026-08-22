"""Data tests over the generated phytochemical_sources seed.

The generator ranks foods by measured concentration. An earlier version ranked
by how many times a compound was measured in a food, which put "Breakfast
cereal" and "Potato" above tomato for lycopene — plausible-looking output that
would have told a sick person to eat the wrong thing. These assertions pin the
handful of compound/food pairs where the right answer is not in dispute, so a
regeneration cannot quietly reintroduce that.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "supabase" / "seeds" / "phytochemical_sources_expanded.sql"

ROW_RE = re.compile(
    r"\(\s*'((?:[^']|'')*)'\s*,\s*ARRAY\[([^\]]*)\]\s*,\s*ARRAY\[([^\]]*)\]\s*,\s*'((?:[^']|'')*)'\s*\)"
)
ITEM_RE = re.compile(r"'((?:[^']|'')*)'")

pytestmark = pytest.mark.skipif(
    not SEED.exists(), reason="run scripts/build_phytochemical_sources.py first"
)


def _rows() -> dict[str, list[str]]:
    text = SEED.read_text(encoding="utf-8")
    out: dict[str, list[str]] = {}
    for m in ROW_RE.finditer(text):
        name = m.group(1).replace("''", "'")
        foods = [i.group(1).replace("''", "'") for i in ITEM_RE.finditer(m.group(2))]
        out[name.lower()] = foods
    return out


@pytest.fixture(scope="module")
def rows() -> dict[str, list[str]]:
    parsed = _rows()
    assert parsed, "seed file parsed to zero rows"
    return parsed


# (compound, a food that must appear) — each is textbook, not borderline.
KNOWN_SOURCES = [
    ("lycopene", "tomato"),
    ("curcumin", "turmeric"),
    ("capsaicin", "pepper"),
    ("resveratrol", "grape"),
    ("hesperidin", "orange"),
    ("3,4-dihydroxyphenylethanol", "olive"),   # hydroxytyrosol
    ("caffeine", "tea"),
]


@pytest.mark.parametrize("compound,expected_food", KNOWN_SOURCES)
def test_known_source_is_present(
    rows: dict[str, list[str]], compound: str, expected_food: str
) -> None:
    foods = rows.get(compound)
    if foods is None:
        pytest.skip(f"{compound} not in this build of the seed")
    joined = " | ".join(foods).lower()
    assert expected_food in joined, f"{compound} -> {foods}"


# Foods that signalled the count-ranking bug. None is a real dietary source of
# lycopene, so their presence means the ranking regressed.
@pytest.mark.parametrize("bogus", ["breakfast cereal", "biscuit", "potato"])
def test_lycopene_has_no_count_artefacts(rows: dict[str, list[str]], bogus: str) -> None:
    foods = rows.get("lycopene")
    if foods is None:
        pytest.skip("lycopene not in this build of the seed")
    assert bogus not in " | ".join(foods).lower(), f"lycopene -> {foods}"


def test_every_row_has_at_least_one_food(rows: dict[str, list[str]]) -> None:
    empty = [name for name, foods in rows.items() if not foods]
    assert not empty, f"rows with no food source: {empty[:5]}"


def test_no_animal_foods_leaked_in(rows: dict[str, list[str]]) -> None:
    """The generator restricts to plant food groups; catch a widened filter.

    Terms are matched on word boundaries and deliberately exclude "milk",
    "yogurt" and "cheese": plant analogues (soy milk, soy yogurt) are genuine
    plant sources — soy milk is one of the better-measured genistein sources —
    and are exactly what this app should be recommending.
    """
    banned = ("beef", "pork", "chicken", "salmon", "tuna", "shrimp", "egg", "eggs", "lard")
    pattern = re.compile(r"\b(" + "|".join(banned) + r")\b", re.IGNORECASE)
    offenders = [
        (name, food)
        for name, foods in rows.items()
        for food in foods
        if pattern.search(food)
    ]
    assert not offenders, f"animal foods present: {offenders[:5]}"


def test_conflict_clause_preserves_curated_rows() -> None:
    # DO UPDATE would overwrite the 85 hand-curated consumer-friendly rows.
    text = SEED.read_text(encoding="utf-8")
    assert "ON CONFLICT (phytochemical_name) DO NOTHING" in text
    assert "DO UPDATE" not in text
