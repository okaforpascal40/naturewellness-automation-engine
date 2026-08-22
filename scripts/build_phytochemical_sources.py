"""Derive phytochemical_sources rows from FooDB measurements.

Every mapping this emits traces to a FooDB `Content` record — an actual
measurement of a compound in a food, with its own citation. Nothing here is
inferred from general knowledge: a compound with no measurement in a plant food
is left out rather than guessed at, because these rows decide what the app
tells a person with a disease to eat.

Inputs
  .cache/ctd_chemicals.json   compound names present in our CTD snapshot
  .cache/foodb/*.csv          FooDB 2020-04-07 bulk export

Output
  supabase/seeds/phytochemical_sources_expanded.sql

Usage
    python scripts/build_phytochemical_sources.py
    python scripts/build_phytochemical_sources.py --min-foods 2
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
FOODB = CACHE / "foodb"
CTD_NAMES = CACHE / "ctd_chemicals.json"
OUTPUT = ROOT / "supabase" / "seeds" / "phytochemical_sources_expanded.sql"

# FooDB groups a food into one of these. Only plant-derived groups qualify:
# this app recommends plant foods, and an "Animal foods" hit would be worse
# than no row at all.
PLANT_FOOD_GROUPS = {
    "fruits",
    "vegetables",
    "herbs and spices",
    "cereals and cereal products",
    "pulses",
    "nuts",
    "gourds",
    "soy",
    "teas",
    "cocoa and cocoa products",
    "coffee and coffee products",
    "beverages",
}

# "Dishes", "Baking goods" and similar are composites (a cake is not a food
# source) — excluded above by omission rather than listed here.

MAX_FOODS = 12          # per phytochemical, ranked by measurement count
MAX_PRIMARY = 3         # highest-evidence subset surfaced separately
DEFAULT_MIN_FOODS = 1   # drop a compound with fewer plant foods than this

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def normalize(name: str) -> str:
    """Fold a chemical name for matching.

    Case, surrounding whitespace and repeated spaces are noise; everything else
    is kept, because in chemistry punctuation is meaning ("beta-carotene" and
    "betacarotene" are the same, but 1,2- and 1,4- are not).
    """
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def loose(name: str) -> str:
    """Aggressive fold used only as a second pass: drop spaces and hyphens."""
    return re.sub(r"[\s\-]", "", normalize(name))


def load_foods() -> dict[str, tuple[str, str]]:
    """food_id -> (display name, food_group), restricted to plant foods."""
    foods: dict[str, tuple[str, str]] = {}
    with (FOODB / "Food.csv").open(encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.DictReader(fh):
            group = (row.get("food_group") or "").strip().lower()
            if group not in PLANT_FOOD_GROUPS:
                continue
            name = (row.get("name") or "").strip()
            fid = (row.get("id") or "").strip()
            if name and fid:
                foods[fid] = (name, group)
    return foods


def load_compounds() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Return (name index, loose name index, compound_id -> chemical class)."""
    by_name: dict[str, str] = {}
    by_loose: dict[str, str] = {}
    klass: dict[str, str] = {}

    with (FOODB / "Compound.csv").open(encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.DictReader(fh):
            cid = (row.get("id") or "").strip()
            name = (row.get("name") or "").strip()
            if not cid or not name:
                continue
            # A name already claimed by an earlier compound wins; FooDB ids are
            # ascending and the lower id is the better-curated parent record.
            by_name.setdefault(normalize(name), cid)
            by_loose.setdefault(loose(name), cid)
            klass[cid] = (row.get("klass") or row.get("superklass") or "").strip()

    with (FOODB / "CompoundSynonym.csv").open(encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("source_type") or "").strip() != "Compound":
                continue
            cid = (row.get("source_id") or "").strip()
            syn = (row.get("synonym") or "").strip()
            if not cid or not syn:
                continue
            by_name.setdefault(normalize(syn), cid)
            by_loose.setdefault(loose(syn), cid)

    return by_name, by_loose, klass


def match_ctd_names(
    ctd_names: list[str],
    by_name: dict[str, str],
    by_loose: dict[str, str],
) -> tuple[dict[str, str], Counter[str]]:
    """CTD name -> FooDB compound id, plus a tally of how each matched."""
    matches: dict[str, str] = {}
    how: Counter[str] = Counter()
    for name in ctd_names:
        cid = by_name.get(normalize(name))
        if cid:
            matches[name] = cid
            how["exact"] += 1
            continue
        cid = by_loose.get(loose(name))
        if cid:
            matches[name] = cid
            how["loose"] += 1
            continue
        how["unmatched"] += 1
    return matches, how


def _as_float(value: str | None) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def collect_contents(
    wanted_ids: set[str],
    foods: dict[str, tuple[str, str]],
) -> dict[str, dict[str, float]]:
    """compound_id -> {food name: peak measured concentration}.

    Ranked by concentration, NOT by how many times a food was measured. FooDB
    records a great many zero and trace readings for ubiquitous foods, so
    counting rows puts "Breakfast cereal" and "Potato" above tomato for
    lycopene. Only rows with a positive standard_content (mg/100g) count, and a
    food is scored by its highest measurement — the peak is what makes a food
    worth recommending as a source.
    """
    out: dict[str, dict[str, float]] = defaultdict(dict)
    # Second tier: a positive reading FooDB never standardised to mg/100g. The
    # measurement is real, its magnitude is not comparable across foods, so it
    # is only used for compounds with no standardised data at all.
    presence: dict[str, Counter[str]] = defaultdict(Counter)
    scanned = 0
    kept = 0
    presence_kept = 0

    with (FOODB / "Content.csv").open(encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.DictReader(fh):
            scanned += 1
            if (row.get("source_type") or "").strip() != "Compound":
                continue
            cid = (row.get("source_id") or "").strip()
            if cid not in wanted_ids:
                continue
            food = foods.get((row.get("food_id") or "").strip())
            if not food:
                continue
            name = food[0]

            content = _as_float(row.get("standard_content"))
            if content is not None and content > 0:
                if content > out[cid].get(name, 0.0):
                    out[cid][name] = content
                kept += 1
                continue

            orig = _as_float(row.get("orig_content"))
            if orig is not None and orig > 0:
                presence[cid][name] += 1
                presence_kept += 1

    print(
        f"  scanned {scanned:,} content rows, kept {kept:,} standardised "
        f"+ {presence_kept:,} presence-only"
    )

    # Promote presence-only evidence for compounds with nothing standardised.
    promoted = 0
    for cid, counter in presence.items():
        if out.get(cid):
            continue
        # Negative scores keep these below any standardised value and preserve
        # most-measured-first ordering within the tier.
        out[cid] = {name: -float(rank) for rank, (name, _) in enumerate(counter.most_common())}
        promoted += 1
    print(f"  compounds carried by presence-only evidence: {promoted:,}")
    return out


def sql_array(values: list[str]) -> str:
    escaped = [v.replace("'", "''") for v in values]
    return "ARRAY[" + ",".join(f"'{v}'" for v in escaped) + "]"


def title_food(name: str) -> str:
    """FooDB food names are already display-cased; just tidy whitespace."""
    return re.sub(r"\s+", " ", name).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-foods", type=int, default=DEFAULT_MIN_FOODS)
    args = parser.parse_args()

    if not CTD_NAMES.exists():
        print(f"ERROR: {CTD_NAMES} missing — export the CTD compound names first.")
        return 1
    if not (FOODB / "Content.csv").exists():
        print(f"ERROR: FooDB CSVs missing under {FOODB}")
        return 1

    ctd_names: list[str] = json.loads(CTD_NAMES.read_text(encoding="utf-8"))
    print(f"CTD compounds: {len(ctd_names):,}")

    print("Loading FooDB foods…")
    foods = load_foods()
    print(f"  plant foods: {len(foods):,}")

    print("Loading FooDB compounds and synonyms…")
    by_name, by_loose, klass = load_compounds()
    print(f"  compound names indexed: {len(by_name):,}")

    matches, how = match_ctd_names(ctd_names, by_name, by_loose)
    print(f"  matched to FooDB: {len(matches):,} "
          f"(exact {how['exact']:,}, loose {how['loose']:,}, unmatched {how['unmatched']:,})")

    print("Streaming Content.csv…")
    contents = collect_contents(set(matches.values()), foods)
    print(f"  compounds with plant-food measurements: {len(contents):,}")

    rows: list[tuple[str, list[str], list[str], str]] = []
    for ctd_name, cid in sorted(matches.items()):
        measured = contents.get(cid)
        if not measured:
            continue
        # Highest measured concentration first.
        ranked = [
            title_food(name)
            for name, _ in sorted(measured.items(), key=lambda kv: kv[1], reverse=True)
        ][:MAX_FOODS]
        if len(ranked) < args.min_foods:
            continue
        rows.append((ctd_name, ranked, ranked[:MAX_PRIMARY], klass.get(cid, "") or "Unclassified"))

    print(f"  rows to emit: {len(rows):,}")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("-- phytochemical_sources, derived from FooDB 2020-04-07 measurements.\n")
        fh.write("-- Generated by scripts/build_phytochemical_sources.py — do not hand-edit.\n")
        fh.write("--\n")
        fh.write("-- Each row's foods come from FooDB Content records (a measured\n")
        fh.write("-- concentration of that compound in that food), restricted to plant food\n")
        fh.write("-- groups and ranked by how many measurements support each food.\n")
        fh.write(f"-- Compounds: {len(rows):,} of {len(ctd_names):,} in the CTD snapshot.\n")
        fh.write("--\n")
        fh.write("-- Safe to re-run: existing rows are left untouched.\n\n")
        fh.write(
            "INSERT INTO phytochemical_sources "
            "(phytochemical_name, fruit_vegetables, primary_sources, chemical_class) VALUES\n"
        )
        for i, (name, all_foods, primary, chem_class) in enumerate(rows):
            terminator = "," if i < len(rows) - 1 else ""
            safe_name = name.replace("'", "''")
            safe_class = chem_class.replace("'", "''")
            fh.write(
                f"('{safe_name}', {sql_array(all_foods)}, "
                f"{sql_array(primary)}, '{safe_class}'){terminator}\n"
            )
        # DO NOTHING, not DO UPDATE: the 85 hand-curated rows use consumer food
        # names ("Onion", "Kale") where FooDB carries botanically precise but
        # less shoppable ones ("Garden onion", "Dock"). Curated rows win; these
        # only fill gaps.
        fh.write("ON CONFLICT (phytochemical_name) DO NOTHING;\n")

    print(f"\nWrote {OUTPUT}")
    print(f"  size: {OUTPUT.stat().st_size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
