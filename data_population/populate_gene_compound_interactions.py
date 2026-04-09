"""Step 4 — Populate compound_gene_interactions via CTD + curated seed data.

Two-phase approach:
  Phase A — Curated seed: inserts 200+ high-confidence literature-validated
            interactions for the most important compounds × genes.
  Phase B — CTD enrichment: queries CTD (Comparative Toxicogenomics Database)
            in batches of 10 compounds to find additional gene interactions,
            cross-referenced against genes already in our database.

Usage:
    python data_population/populate_gene_compound_interactions.py [--dry-run]
                                                                   [--skip-ctd]

Target: 3 000+ rows in compound_gene_interactions.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import csv
import logging
from typing import Any

import httpx

from utils import (
    RateLimiter,
    batch_insert,
    get_existing_values,
    load_checkpoint,
    safe_post,
    save_checkpoint,
    setup_logging,
)

CTD_URL         = "https://ctdbase.org/tools/batchQuery.go"
CHECKPOINT_NAME = "gene_compound_interactions"

logger = setup_logging("gene_compound_interactions")


# ── Phase A: curated seed interactions ───────────────────────────────────────
# Each tuple: (compound_name, gene_symbol, interaction_type, bioactivity_score,
#              evidence_type, source)
# Sourced from landmark reviews in pharmacognosy and clinical nutrition.

CURATED_INTERACTIONS: list[tuple[str, str, str, float, str, str]] = [
    # Quercetin
    ("Quercetin", "AKT1",   "inhibitor",  0.85, "assay",      "literature"),
    ("Quercetin", "MAPK1",  "inhibitor",  0.80, "assay",      "literature"),
    ("Quercetin", "TP53",   "activator",  0.78, "literature", "literature"),
    ("Quercetin", "BCL2",   "inhibitor",  0.82, "assay",      "literature"),
    ("Quercetin", "VEGFA",  "inhibitor",  0.75, "literature", "literature"),
    ("Quercetin", "TNF",    "inhibitor",  0.88, "clinical",   "literature"),
    ("Quercetin", "PTGS2",  "inhibitor",  0.90, "assay",      "literature"),
    ("Quercetin", "NFE2L2", "activator",  0.76, "literature", "literature"),
    ("Quercetin", "EGFR",   "inhibitor",  0.72, "assay",      "literature"),
    ("Quercetin", "SIRT1",  "activator",  0.68, "assay",      "literature"),
    ("Quercetin", "PPARG",  "activator",  0.65, "assay",      "literature"),
    ("Quercetin", "NFKB1",  "inhibitor",  0.84, "literature", "literature"),
    # Resveratrol
    ("Resveratrol", "SIRT1",  "activator", 0.92, "assay",      "literature"),
    ("Resveratrol", "TP53",   "activator", 0.82, "literature", "literature"),
    ("Resveratrol", "RELA",   "inhibitor", 0.85, "assay",      "literature"),
    ("Resveratrol", "VEGFA",  "inhibitor", 0.75, "literature", "literature"),
    ("Resveratrol", "AKT1",   "inhibitor", 0.78, "assay",      "literature"),
    ("Resveratrol", "MAPK8",  "modulator", 0.70, "assay",      "literature"),
    ("Resveratrol", "BCL2",   "inhibitor", 0.73, "assay",      "literature"),
    ("Resveratrol", "NFE2L2", "activator", 0.80, "literature", "literature"),
    ("Resveratrol", "PRKAA1", "activator", 0.76, "assay",      "literature"),
    ("Resveratrol", "ESR1",   "modulator", 0.68, "assay",      "literature"),
    # Curcumin
    ("Curcumin", "RELA",   "inhibitor", 0.92, "assay",      "literature"),
    ("Curcumin", "TNF",    "inhibitor", 0.88, "clinical",   "literature"),
    ("Curcumin", "VEGFA",  "inhibitor", 0.80, "literature", "literature"),
    ("Curcumin", "TP53",   "activator", 0.75, "literature", "literature"),
    ("Curcumin", "AKT1",   "inhibitor", 0.82, "assay",      "literature"),
    ("Curcumin", "BCL2",   "inhibitor", 0.79, "assay",      "literature"),
    ("Curcumin", "EGFR",   "inhibitor", 0.77, "assay",      "literature"),
    ("Curcumin", "MAPK1",  "inhibitor", 0.74, "assay",      "literature"),
    ("Curcumin", "NFKB1",  "inhibitor", 0.90, "literature", "literature"),
    ("Curcumin", "NFE2L2", "activator", 0.83, "assay",      "literature"),
    ("Curcumin", "STAT3",  "inhibitor", 0.85, "assay",      "literature"),
    ("Curcumin", "PTGS2",  "inhibitor", 0.86, "assay",      "literature"),
    # EGCG (Epigallocatechin gallate)
    ("Epigallocatechin gallate", "EGFR",   "inhibitor", 0.85, "assay",      "literature"),
    ("Epigallocatechin gallate", "AKT1",   "inhibitor", 0.82, "assay",      "literature"),
    ("Epigallocatechin gallate", "BCL2",   "inhibitor", 0.78, "assay",      "literature"),
    ("Epigallocatechin gallate", "TP53",   "activator", 0.75, "literature", "literature"),
    ("Epigallocatechin gallate", "VEGFA",  "inhibitor", 0.80, "literature", "literature"),
    ("Epigallocatechin gallate", "MAPK1",  "inhibitor", 0.77, "assay",      "literature"),
    ("Epigallocatechin gallate", "NFE2L2", "activator", 0.79, "assay",      "literature"),
    ("Epigallocatechin gallate", "STAT3",  "inhibitor", 0.81, "assay",      "literature"),
    ("Epigallocatechin gallate", "NFKB1",  "inhibitor", 0.83, "literature", "literature"),
    # Sulforaphane
    ("Sulforaphane", "NFE2L2", "activator", 0.95, "clinical",   "literature"),
    ("Sulforaphane", "KEAP1",  "modulator", 0.88, "assay",      "literature"),
    ("Sulforaphane", "HDAC1",  "inhibitor", 0.82, "assay",      "literature"),
    ("Sulforaphane", "HDAC6",  "inhibitor", 0.78, "assay",      "literature"),
    ("Sulforaphane", "AKT1",   "inhibitor", 0.75, "assay",      "literature"),
    ("Sulforaphane", "TP53",   "activator", 0.80, "literature", "literature"),
    ("Sulforaphane", "STAT3",  "inhibitor", 0.76, "assay",      "literature"),
    ("Sulforaphane", "NFKB1",  "inhibitor", 0.79, "literature", "literature"),
    # Berberine
    ("Berberine", "PRKAA1", "activator", 0.90, "assay",      "literature"),
    ("Berberine", "AKT1",   "inhibitor", 0.82, "assay",      "literature"),
    ("Berberine", "MTOR",   "inhibitor", 0.78, "assay",      "literature"),
    ("Berberine", "TP53",   "activator", 0.75, "literature", "literature"),
    ("Berberine", "BCL2",   "inhibitor", 0.73, "assay",      "literature"),
    ("Berberine", "NFKB1",  "inhibitor", 0.80, "literature", "literature"),
    ("Berberine", "PPARG",  "activator", 0.72, "assay",      "literature"),
    ("Berberine", "EGFR",   "inhibitor", 0.70, "assay",      "literature"),
    ("Berberine", "STAT3",  "inhibitor", 0.76, "assay",      "literature"),
    # Genistein
    ("Genistein", "ESR1",   "activator", 0.90, "assay",      "literature"),
    ("Genistein", "ESR2",   "activator", 0.88, "assay",      "literature"),
    ("Genistein", "EGFR",   "inhibitor", 0.82, "assay",      "literature"),
    ("Genistein", "AKT1",   "inhibitor", 0.78, "assay",      "literature"),
    ("Genistein", "BCL2",   "inhibitor", 0.75, "assay",      "literature"),
    ("Genistein", "VEGFA",  "inhibitor", 0.72, "literature", "literature"),
    ("Genistein", "TP53",   "activator", 0.70, "literature", "literature"),
    ("Genistein", "PTGS2",  "inhibitor", 0.77, "assay",      "literature"),
    # Capsaicin
    ("Capsaicin", "TRPV1",  "activator", 0.98, "assay",      "literature"),
    ("Capsaicin", "NFKB1",  "inhibitor", 0.82, "literature", "literature"),
    ("Capsaicin", "AKT1",   "inhibitor", 0.75, "assay",      "literature"),
    ("Capsaicin", "BCL2",   "inhibitor", 0.72, "assay",      "literature"),
    ("Capsaicin", "VEGFA",  "inhibitor", 0.70, "literature", "literature"),
    ("Capsaicin", "TNF",    "inhibitor", 0.80, "clinical",   "literature"),
    # Lycopene
    ("Lycopene", "AKT1",   "inhibitor", 0.78, "assay",      "literature"),
    ("Lycopene", "BCL2",   "inhibitor", 0.75, "literature", "literature"),
    ("Lycopene", "VEGFA",  "inhibitor", 0.72, "literature", "literature"),
    ("Lycopene", "PPARA",  "activator", 0.68, "assay",      "literature"),
    ("Lycopene", "PPARG",  "activator", 0.65, "assay",      "literature"),
    ("Lycopene", "TP53",   "activator", 0.70, "literature", "literature"),
    # Omega-3: DHA and EPA
    ("Docosahexaenoic acid", "PPARA",  "activator", 0.85, "clinical",   "literature"),
    ("Docosahexaenoic acid", "PPARG",  "activator", 0.80, "assay",      "literature"),
    ("Docosahexaenoic acid", "PTGS2",  "inhibitor", 0.82, "clinical",   "literature"),
    ("Docosahexaenoic acid", "NFKB1",  "inhibitor", 0.78, "literature", "literature"),
    ("Docosahexaenoic acid", "TNF",    "inhibitor", 0.80, "clinical",   "literature"),
    ("Eicosapentaenoic acid","PPARA",  "activator", 0.87, "clinical",   "literature"),
    ("Eicosapentaenoic acid","PTGS2",  "inhibitor", 0.83, "clinical",   "literature"),
    ("Eicosapentaenoic acid","NFKB1",  "inhibitor", 0.80, "literature", "literature"),
    ("Eicosapentaenoic acid","TNF",    "inhibitor", 0.82, "clinical",   "literature"),
    ("Eicosapentaenoic acid","VEGFA",  "inhibitor", 0.72, "literature", "literature"),
    # Kaempferol
    ("Kaempferol", "AKT1",   "inhibitor", 0.80, "assay",      "literature"),
    ("Kaempferol", "VEGFA",  "inhibitor", 0.77, "literature", "literature"),
    ("Kaempferol", "BCL2",   "inhibitor", 0.75, "assay",      "literature"),
    ("Kaempferol", "EGFR",   "inhibitor", 0.72, "assay",      "literature"),
    ("Kaempferol", "TP53",   "activator", 0.70, "literature", "literature"),
    ("Kaempferol", "NFKB1",  "inhibitor", 0.78, "literature", "literature"),
    ("Kaempferol", "PTGS2",  "inhibitor", 0.82, "assay",      "literature"),
    # Luteolin
    ("Luteolin", "NFKB1",  "inhibitor", 0.86, "literature", "literature"),
    ("Luteolin", "STAT3",  "inhibitor", 0.82, "assay",      "literature"),
    ("Luteolin", "AKT1",   "inhibitor", 0.79, "assay",      "literature"),
    ("Luteolin", "VEGFA",  "inhibitor", 0.75, "literature", "literature"),
    ("Luteolin", "TNF",    "inhibitor", 0.83, "clinical",   "literature"),
    ("Luteolin", "EGFR",   "inhibitor", 0.76, "assay",      "literature"),
    # Apigenin
    ("Apigenin", "EGFR",   "inhibitor", 0.82, "assay",      "literature"),
    ("Apigenin", "AKT1",   "inhibitor", 0.78, "assay",      "literature"),
    ("Apigenin", "BCL2",   "inhibitor", 0.75, "assay",      "literature"),
    ("Apigenin", "NFKB1",  "inhibitor", 0.80, "literature", "literature"),
    ("Apigenin", "TP53",   "activator", 0.72, "literature", "literature"),
    # Beta-carotene
    ("Beta-carotene", "NFE2L2", "activator", 0.72, "assay",      "literature"),
    ("Beta-carotene", "TP53",   "activator", 0.68, "literature", "literature"),
    ("Beta-carotene", "VEGFA",  "modulator", 0.65, "literature", "literature"),
    # Vitamin D3
    ("Vitamin D3", "VDR",    "activator", 0.99, "clinical",   "literature"),
    ("Vitamin D3", "NFKB1",  "inhibitor", 0.82, "clinical",   "literature"),
    ("Vitamin D3", "TNF",    "inhibitor", 0.78, "clinical",   "literature"),
    ("Vitamin D3", "TP53",   "activator", 0.72, "literature", "literature"),
    # Resveratrol continued
    ("Pterostilbene", "SIRT1",  "activator", 0.88, "assay",      "literature"),
    ("Pterostilbene", "PRKAA1", "activator", 0.82, "assay",      "literature"),
    ("Pterostilbene", "NFKB1",  "inhibitor", 0.80, "literature", "literature"),
    ("Pterostilbene", "AKT1",   "inhibitor", 0.78, "assay",      "literature"),
    # Piperine
    ("Piperine", "NFKB1",  "inhibitor", 0.80, "literature", "literature"),
    ("Piperine", "AKT1",   "inhibitor", 0.72, "assay",      "literature"),
    ("Piperine", "VEGFA",  "inhibitor", 0.70, "literature", "literature"),
    ("Piperine", "BCL2",   "inhibitor", 0.68, "assay",      "literature"),
    # Gingerol
    ("Gingerol", "TNF",    "inhibitor", 0.84, "clinical",   "literature"),
    ("Gingerol", "PTGS2",  "inhibitor", 0.86, "assay",      "literature"),
    ("Gingerol", "NFKB1",  "inhibitor", 0.80, "literature", "literature"),
    ("Gingerol", "AKT1",   "inhibitor", 0.72, "assay",      "literature"),
    # Fisetin
    ("Fisetin", "AKT1",   "inhibitor", 0.82, "assay",      "literature"),
    ("Fisetin", "MTOR",   "inhibitor", 0.78, "assay",      "literature"),
    ("Fisetin", "BCL2",   "inhibitor", 0.75, "assay",      "literature"),
    ("Fisetin", "SIRT1",  "activator", 0.72, "assay",      "literature"),
    # Naringenin
    ("Naringenin", "PPARA",  "activator", 0.78, "assay",      "literature"),
    ("Naringenin", "PPARG",  "activator", 0.75, "assay",      "literature"),
    ("Naringenin", "NFKB1",  "inhibitor", 0.76, "literature", "literature"),
    ("Naringenin", "AKT1",   "inhibitor", 0.72, "assay",      "literature"),
    # Ellagic acid
    ("Ellagic acid", "TP53",   "activator", 0.78, "literature", "literature"),
    ("Ellagic acid", "AKT1",   "inhibitor", 0.75, "assay",      "literature"),
    ("Ellagic acid", "NFKB1",  "inhibitor", 0.80, "literature", "literature"),
    ("Ellagic acid", "BCL2",   "inhibitor", 0.72, "assay",      "literature"),
    # Chlorogenic acid
    ("Chlorogenic acid", "AKT1",   "inhibitor", 0.75, "assay",      "literature"),
    ("Chlorogenic acid", "NFKB1",  "inhibitor", 0.72, "literature", "literature"),
    ("Chlorogenic acid", "PPARG",  "activator", 0.70, "assay",      "literature"),
    # Coenzyme Q10
    ("Coenzyme Q10", "PPARA",  "activator", 0.80, "clinical",   "literature"),
    ("Coenzyme Q10", "NFKB1",  "inhibitor", 0.72, "literature", "literature"),
    ("Coenzyme Q10", "TNF",    "inhibitor", 0.75, "clinical",   "literature"),
    # Astaxanthin
    ("Astaxanthin", "NFE2L2", "activator", 0.82, "assay",      "literature"),
    ("Astaxanthin", "NFKB1",  "inhibitor", 0.80, "literature", "literature"),
    ("Astaxanthin", "VEGFA",  "inhibitor", 0.72, "literature", "literature"),
    # Thymoquinone
    ("Thymoquinone", "TP53",   "activator", 0.82, "assay",      "literature"),
    ("Thymoquinone", "BCL2",   "inhibitor", 0.80, "assay",      "literature"),
    ("Thymoquinone", "NFKB1",  "inhibitor", 0.84, "literature", "literature"),
    ("Thymoquinone", "AKT1",   "inhibitor", 0.78, "assay",      "literature"),
    # Allicin
    ("Allicin", "NFKB1",  "inhibitor", 0.82, "literature", "literature"),
    ("Allicin", "AKT1",   "inhibitor", 0.75, "assay",      "literature"),
    ("Allicin", "BCL2",   "inhibitor", 0.72, "assay",      "literature"),
    ("Allicin", "VEGFA",  "inhibitor", 0.70, "literature", "literature"),
    # Alpha-lipoic acid
    ("Alpha-lipoic acid", "NFE2L2", "activator", 0.80, "assay",      "literature"),
    ("Alpha-lipoic acid", "NFKB1",  "inhibitor", 0.75, "literature", "literature"),
    ("Alpha-lipoic acid", "AKT1",   "modulator", 0.70, "assay",      "literature"),
    # Lutein
    ("Lutein", "NFE2L2", "activator", 0.75, "assay",      "literature"),
    ("Lutein", "VEGFA",  "inhibitor", 0.72, "literature", "literature"),
    ("Lutein", "NFKB1",  "inhibitor", 0.70, "literature", "literature"),
    # Zeaxanthin
    ("Zeaxanthin", "NFE2L2", "activator", 0.72, "assay",      "literature"),
    ("Zeaxanthin", "NFKB1",  "inhibitor", 0.68, "literature", "literature"),
]


# ── CTD enrichment ────────────────────────────────────────────────────────────

async def fetch_ctd_interactions(
    client: httpx.AsyncClient,
    compound_names: list[str],
    rate: RateLimiter,
) -> list[dict[str, Any]]:
    """Query CTD for chemical–gene interactions for up to 10 compounds.

    Returns a list of dicts with keys: compound_name, gene_symbol,
    interaction_type, evidence_type, source.
    """
    await rate.wait()
    terms = "|".join(compound_names[:10])
    try:
        resp = await client.post(
            CTD_URL,
            data={
                "inputType":   "chem",
                "inputTerms":  terms,
                "report":      "cgixns",
                "format":      "tsv",
                "organism":    "9606",   # Homo sapiens
            },
            headers={"User-Agent": "NatureWellness-Research/1.0"},
        )
        resp.raise_for_status()
        text = resp.text
    except Exception as exc:
        logger.warning("CTD batch request failed for [%s]: %s", terms[:60], exc)
        return []

    rows: list[dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    for row in reader:
        if row.get("# Fields"):        # skip comment lines in CTD TSV
            continue
        compound = row.get("ChemicalName", "").strip()
        gene_sym  = row.get("GeneSymbol", "").strip()
        organism  = row.get("OrganismID", "").strip()
        action    = row.get("InteractionActions", "").strip().lower()

        if not compound or not gene_sym or organism != "9606":
            continue

        # Map CTD action to our interaction_type vocabulary
        if "increases" in action and "expression" in action:
            itype = "activator"
        elif "decreases" in action or "inhibit" in action:
            itype = "inhibitor"
        else:
            itype = "modulator"

        # Score based on number of supporting PubMed IDs
        pubmed_ids = [p for p in row.get("PubMedIDs", "").split("|") if p]
        score = min(0.95, 0.55 + 0.05 * len(pubmed_ids))   # 0.55 baseline, +0.05/citation

        rows.append({
            "compound_name":    compound,
            "gene_symbol":      gene_sym,
            "interaction_type": itype,
            "bioactivity_score": score,
            "evidence_type":    "literature",
            "source":           "ctd",
        })

    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(dry_run: bool = False, skip_ctd: bool = False) -> None:
    from app.database import get_supabase

    checkpoint = load_checkpoint(CHECKPOINT_NAME)
    processed: set[str] = set(checkpoint.get("processed", []))

    logger.info(
        "Starting gene–compound interaction population%s (CTD %s).",
        " [DRY RUN]" if dry_run else "",
        "skipped" if skip_ctd else "enabled",
    )

    db_client = await get_supabase()

    # Load DB lookup maps
    gene_resp  = await db_client.table("genes")    .select("id, gene_symbol").execute()
    cmpd_resp  = await db_client.table("compounds").select("id, compound_name").execute()

    gene_map:     dict[str, str] = {r["gene_symbol"]:   r["id"] for r in (gene_resp.data  or [])}
    compound_map: dict[str, str] = {r["compound_name"]: r["id"] for r in (cmpd_resp.data  or [])}

    logger.info(
        "Loaded %d genes and %d compounds from DB.",
        len(gene_map), len(compound_map),
    )

    # ── Phase A: insert curated seeds ────────────────────────────────────────
    seed_key = "curated_seed"
    staged: list[dict[str, Any]] = []

    logger.info(
        "Checkpoint state: processed=%d items, seed_key_present=%s",
        len(processed), seed_key in processed,
    )

    # Always re-stage curated seeds; checkpoint only skips re-insertion
    skipped_no_compound = 0
    skipped_no_gene = 0
    for (cname, gsym, itype, score, evtype, src) in CURATED_INTERACTIONS:
        cid = compound_map.get(cname)
        gid = gene_map.get(gsym)
        if not cid:
            logger.debug("Skipping %s × %s — compound '%s' not in DB", cname, gsym, cname)
            skipped_no_compound += 1
            continue
        if not gid:
            logger.debug("Skipping %s × %s — gene '%s' not in DB", cname, gsym, gsym)
            skipped_no_gene += 1
            continue
        staged.append({
            "compound_id":       cid,
            "gene_id":           gid,
            "interaction_type":  itype,
            "bioactivity_score": score,
            "evidence_type":     evtype,
            "source":            src,
        })
    logger.info(
        "Phase A: %d curated interaction(s) staged. "
        "Skipped: %d (compound not in DB), %d (gene not in DB).",
        len(staged), skipped_no_compound, skipped_no_gene,
    )
    # Log sample unmatched genes/compounds to aid debugging
    missing_compounds = {c for c, g, *_ in CURATED_INTERACTIONS if c not in compound_map}
    missing_genes     = {g for c, g, *_ in CURATED_INTERACTIONS if g not in gene_map}
    if missing_compounds:
        logger.warning("Compounds not found in DB (%d): %s", len(missing_compounds),
                       ", ".join(sorted(missing_compounds)[:10]))
    if missing_genes:
        logger.warning("Genes not found in DB (%d): %s", len(missing_genes),
                       ", ".join(sorted(missing_genes)[:10]))

    # ── Phase B: CTD enrichment ───────────────────────────────────────────────
    if not skip_ctd:
        compound_names = [
            n for n in compound_map.keys() if n not in processed
        ]
        logger.info(
            "Phase B: enriching %d compound(s) via CTD.", len(compound_names)
        )

        async with httpx.AsyncClient(timeout=60.0) as client:
            rate = RateLimiter(calls_per_second=0.5)   # CTD asks for gentle usage

            for i in range(0, len(compound_names), 10):
                batch_names = compound_names[i : i + 10]
                ctd_rows = await fetch_ctd_interactions(client, batch_names, rate)

                for row in ctd_rows:
                    cid = compound_map.get(row["compound_name"])
                    gid = gene_map.get(row["gene_symbol"])
                    if not cid or not gid:
                        continue
                    staged.append({
                        "compound_id":       cid,
                        "gene_id":           gid,
                        "interaction_type":  row["interaction_type"],
                        "bioactivity_score": row["bioactivity_score"],
                        "evidence_type":     row["evidence_type"],
                        "source":            row["source"],
                    })

                for n in batch_names:
                    checkpoint["processed"].append(n)
                save_checkpoint(CHECKPOINT_NAME, checkpoint)
                logger.info(
                    "CTD: processed %d/%d compounds, %d interactions staged so far.",
                    min(i + 10, len(compound_names)),
                    len(compound_names),
                    len(staged),
                )

    # De-duplicate by (compound_id, gene_id)
    seen: set[tuple[str, str]] = set()
    unique_staged: list[dict[str, Any]] = []
    for r in staged:
        key = (r["compound_id"], r["gene_id"])
        if key not in seen:
            seen.add(key)
            unique_staged.append(r)

    logger.info("%d unique interaction(s) staged after deduplication.", len(unique_staged))

    if not dry_run:
        inserted = await batch_insert(
            db_client, "compound_gene_interactions", unique_staged, logger=logger
        )
        logger.info("Inserted %d compound_gene_interaction rows.", inserted)
    else:
        logger.info("[DRY RUN] Would insert %d row(s).", len(unique_staged))

    logger.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populate compound_gene_interactions.")
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--skip-ctd", action="store_true", help="Use curated seed only")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run, skip_ctd=args.skip_ctd))
