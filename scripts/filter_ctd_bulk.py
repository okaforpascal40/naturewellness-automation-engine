"""Filter CTD's chemical-gene interactions bulk file to natural compounds.

Two passes over CTD's public downloads:

  Pass 1 — CTD_chemicals.tsv.gz (the MeSH-derived chemical vocabulary) builds
           the set of ChemicalIDs that count as natural compounds. A chemical
           qualifies on any one of:
             a. its MeSH tree position sits under one of NATURAL_CLASSES
             b. its name contains "natural", "plant" or "phytochemical"
             c. it appears in our phytochemical_sources table
  Pass 2 — CTD_chem_gene_ixns.tsv.gz keeps rows that are human (OrganismID
           9606), carry a non-empty InteractionActions, and whose ChemicalID
           passed pass 1.

Class membership comes from the vocabulary itself rather than hardcoded MeSH
numbers: we look up each class descriptor by name, read its TreeNumbers, and
treat those as prefixes. If CTD renumbers a branch this keeps working.

Downloads are cached under .cache/ (gitignored) so re-runs are cheap.

Usage:
    python scripts/filter_ctd_bulk.py
    python scripts/filter_ctd_bulk.py --refresh   # re-download the source files
"""
from __future__ import annotations

import argparse
import gzip
import os
import re
import shutil
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHEMICALS_URL = "https://ctdbase.org/reports/CTD_chemicals.tsv.gz"
INTERACTIONS_URL = "https://ctdbase.org/reports/CTD_chem_gene_ixns.tsv.gz"

CACHE_DIR = ROOT / ".cache"
OUTPUT_PATH = ROOT / "scripts" / "ctd_phytochemical_interactions_filtered.tsv"
SOURCES_SQL = ROOT / "supabase" / "seeds" / "phytochemical_sources.sql"

USER_AGENT = "naturewellness-automation-engine/2.0 (+filter)"

# CTD column order per http://ctdbase.org/downloads/#cg
CTD_COLUMNS = [
    "ChemicalName",
    "ChemicalID",
    "CasRN",
    "GeneSymbol",
    "GeneID",
    "GeneForms",
    "Organism",
    "OrganismID",
    "Interaction",
    "InteractionActions",
    "PubMedIDs",
]

# Column offsets in CTD_chemicals.tsv.gz
CHEM_NAME, CHEM_ID, CHEM_TREES = 0, 1, 9
CHEM_MESH_SYNONYMS, CHEM_CTD_SYNONYMS = 11, 12

HUMAN_TAXON = "9606"


def normalize_chemical_id(raw: str) -> str:
    """Strip the vocabulary's `MESH:` prefix.

    CTD_chemicals.tsv.gz writes "MESH:D000470" while CTD_chem_gene_ixns.tsv.gz
    writes the bare "D000470" for the same chemical, so the two files only join
    after normalising.
    """
    value = (raw or "").strip()
    _, _, suffix = value.partition(":")
    return suffix or value

# MeSH descriptors whose subtrees we treat as natural-compound classes. Names
# must match the vocabulary's ChemicalName exactly (case-insensitively).
NATURAL_CLASSES = (
    "Flavonoids",
    "Polyphenols",
    "Terpenes",           # MeSH's descriptor for terpenoids
    "Alkaloids",
    "Carotenoids",
    "Glucosinolates",
    "Phytosterols",
    "Saponins",
    "Anthocyanins",
    "Isoflavones",
    "Lignans",
    "Stilbenes",
    "Tannins",
    "Coumarins",
    "Quinones",
    "Oils, Volatile",     # MeSH's descriptor for essential oils
    "Phytochemicals",
)

NAME_KEYWORDS = ("natural", "plant", "phytochemical")

# Reused from scripts/seed_phytochemical_sources.py — matches one seed row:
# ('Name', ARRAY[...], ARRAY[...], 'Class')
SOURCES_ROW_RE = re.compile(r"\(\s*'((?:[^']|'')+)'\s*,\s*ARRAY\[", re.MULTILINE)


def download(url: str, dest: Path, refresh: bool) -> Path:
    """Fetch `url` into `dest`, reusing the cached copy unless refreshing."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not refresh:
        print(f"  using cached {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
        return dest

    print(f"  downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=600) as resp, tmp.open("wb") as fh:
        shutil.copyfileobj(resp, fh)
    tmp.replace(dest)
    print(f"  saved {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
    return dest


def load_source_phytochemicals() -> set[str]:
    """Lowercased names from phytochemical_sources.

    Prefers the live Supabase table so manual additions are honoured, and falls
    back to the seed SQL that populates it when credentials are absent.
    """
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if url and key:
        try:
            from supabase import create_client  # type: ignore[import-not-found]

            client = create_client(url, key)
            rows = client.table("phytochemical_sources").select("phytochemical_name").execute()
            names = {
                (r.get("phytochemical_name") or "").strip().lower()
                for r in (rows.data or [])
            }
            names.discard("")
            if names:
                print(f"  phytochemical_sources: {len(names)} name(s) from Supabase")
                return names
            print("  phytochemical_sources: Supabase returned no rows, using seed SQL")
        except Exception as exc:  # noqa: BLE001 - any failure falls back to the seed
            print(f"  phytochemical_sources: Supabase unavailable ({exc}), using seed SQL")

    if not SOURCES_SQL.exists():
        print(f"  phytochemical_sources: {SOURCES_SQL} missing, skipping this rule")
        return set()

    text = SOURCES_SQL.read_text(encoding="utf-8")
    names = {m.group(1).replace("''", "'").strip().lower() for m in SOURCES_ROW_RE.finditer(text)}
    names.discard("")
    print(f"  phytochemical_sources: {len(names)} name(s) from {SOURCES_SQL.name}")
    return names


def resolve_class_prefixes(chemicals_path: Path) -> dict[str, tuple[str, ...]]:
    """Map each NATURAL_CLASSES descriptor to its MeSH tree number prefixes."""
    wanted = {name.lower(): name for name in NATURAL_CLASSES}
    found: dict[str, tuple[str, ...]] = {}

    with gzip.open(chemicals_path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) <= CHEM_TREES:
                continue
            canonical = wanted.get(parts[CHEM_NAME].strip().lower())
            if canonical is None:
                continue
            trees = tuple(t.strip() for t in parts[CHEM_TREES].split("|") if t.strip())
            if trees:
                found[canonical] = trees

    for name in NATURAL_CLASSES:
        if name not in found:
            print(f"  WARNING: class descriptor {name!r} not found in the vocabulary")
    return found


def build_natural_chemical_ids(
    chemicals_path: Path,
    source_names: set[str],
) -> tuple[set[str], Counter[str]]:
    """Return the ChemicalIDs judged natural, plus a per-rule match tally."""
    class_prefixes = resolve_class_prefixes(chemicals_path)
    print(f"  resolved {len(class_prefixes)}/{len(NATURAL_CLASSES)} class descriptors")
    for name, trees in sorted(class_prefixes.items()):
        print(f"    {name:16} {'|'.join(trees)}")

    # Flatten to a single tuple for str.startswith, which accepts a tuple.
    prefixes = tuple(t for trees in class_prefixes.values() for t in trees)

    allowed: set[str] = set()
    reasons: Counter[str] = Counter()
    scanned = 0

    with gzip.open(chemicals_path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) <= CHEM_TREES:
                continue
            scanned += 1

            chem_id = normalize_chemical_id(parts[CHEM_ID])
            if not chem_id:
                continue
            name = parts[CHEM_NAME].strip()
            lname = name.lower()

            # (a) MeSH class membership
            trees = [t.strip() for t in parts[CHEM_TREES].split("|") if t.strip()]
            if prefixes and any(t.startswith(prefixes) for t in trees):
                allowed.add(chem_id)
                reasons["mesh_class"] += 1
                continue

            # (b) name keyword
            if any(kw in lname for kw in NAME_KEYWORDS):
                allowed.add(chem_id)
                reasons["name_keyword"] += 1
                continue

            # (c) present in phytochemical_sources, by name or any synonym
            if source_names:
                if lname in source_names:
                    allowed.add(chem_id)
                    reasons["sources_table"] += 1
                    continue
                synonyms = set()
                for idx in (CHEM_MESH_SYNONYMS, CHEM_CTD_SYNONYMS):
                    if len(parts) > idx:
                        synonyms.update(
                            s.strip().lower() for s in parts[idx].split("|") if s.strip()
                        )
                if synonyms & source_names:
                    allowed.add(chem_id)
                    reasons["sources_table_synonym"] += 1

    print(f"  scanned {scanned:,} vocabulary entries")
    return allowed, reasons


def filter_interactions(interactions_path: Path, allowed: set[str]) -> dict[str, int]:
    """Stream the interactions file, writing the kept rows to OUTPUT_PATH."""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    stats = {
        "read": 0,
        "kept": 0,
        "drop_organism": 0,
        "drop_no_actions": 0,
        "drop_not_natural": 0,
    }
    chemicals_kept: set[str] = set()
    genes_kept: set[str] = set()
    start = time.time()
    last_log = start

    with gzip.open(interactions_path, "rt", encoding="utf-8", errors="replace") as fh, \
            OUTPUT_PATH.open("w", encoding="utf-8", newline="") as out:
        out.write("\t".join(CTD_COLUMNS) + "\n")

        for line in fh:
            if not line or line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) < len(CTD_COLUMNS):
                continue
            stats["read"] += 1

            if fields[7].strip() != HUMAN_TAXON:
                stats["drop_organism"] += 1
                continue
            if not fields[9].strip():
                stats["drop_no_actions"] += 1
                continue
            if normalize_chemical_id(fields[1]) not in allowed:
                stats["drop_not_natural"] += 1
                continue

            out.write("\t".join(fields[: len(CTD_COLUMNS)]) + "\n")
            stats["kept"] += 1
            chemicals_kept.add(fields[0].strip())
            genes_kept.add(fields[3].strip())

            now = time.time()
            if now - last_log >= 5.0:
                print(
                    f"  ... read {stats['read']:>10,} | kept {stats['kept']:>9,}"
                    f" | {now - start:>5.1f}s",
                    flush=True,
                )
                last_log = now

    stats["unique_chemicals"] = len(chemicals_kept)
    stats["unique_genes"] = len(genes_kept)
    stats["seconds"] = int(time.time() - start)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-download the CTD source files instead of using the cache",
    )
    args = parser.parse_args()

    print("Step 1/3 — source files")
    chemicals_path = download(CHEMICALS_URL, CACHE_DIR / "CTD_chemicals.tsv.gz", args.refresh)
    interactions_path = download(
        INTERACTIONS_URL, CACHE_DIR / "CTD_chem_gene_ixns.tsv.gz", args.refresh
    )

    print()
    print("Step 2/3 — natural-compound vocabulary")
    source_names = load_source_phytochemicals()
    allowed, reasons = build_natural_chemical_ids(chemicals_path, source_names)
    print(f"  natural chemicals: {len(allowed):,}")
    for reason, count in reasons.most_common():
        print(f"    {reason:24} {count:>8,}")

    print()
    print("Step 3/3 — filtering interactions")
    stats = filter_interactions(interactions_path, allowed)

    print()
    print(f"Done in {stats['seconds']}s")
    print(f"  Rows read          : {stats['read']:,}")
    print(f"  Rows kept          : {stats['kept']:,}")
    print(f"    dropped non-human: {stats['drop_organism']:,}")
    print(f"    dropped no action: {stats['drop_no_actions']:,}")
    print(f"    dropped non-natural: {stats['drop_not_natural']:,}")
    print(f"  Unique chemicals   : {stats['unique_chemicals']:,}")
    print(f"  Unique genes       : {stats['unique_genes']:,}")
    print(f"  Output             : {OUTPUT_PATH}")
    print(f"  Output size        : {OUTPUT_PATH.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
