"""Step 4b — Expand compound_gene_interactions from KEGG.

For each gene in the database, queries KEGG /link/compound to find all
metabolites that interact with it, then matches those metabolites against
our existing compounds table by name (case-insensitive, with alias expansion).
Only C-prefix KEGG IDs are kept — D-prefix drugs are skipped.

Usage:
    python data_population/populate_kegg_compounds.py [--dry-run] [--batch-size 50]

Target: 500+ rows in compound_gene_interactions.
"""
from __future__ import annotations

import argparse
import asyncio
import re
from typing import Any

import httpx

from utils import (
    RateLimiter,
    batch_insert,
    load_checkpoint,
    save_checkpoint,
    setup_logging,
)

CHECKPOINT_NAME  = "kegg_compounds"
KEGG_BASE        = "https://rest.kegg.jp"
CONFIDENCE_SCORE = 0.72   # KEGG-derived interactions — solid but less curated than seed

logger = setup_logging("kegg_compounds")


# ── KEGG name → our compound_name aliases ─────────────────────────────────────
# KEGG often returns systematic or alternate names. Map them to the names in
# our compounds table. Keys are lowercase KEGG primary names (or fragments);
# values are the exact compound_name strings in our DB.

KEGG_NAME_ALIASES: dict[str, str] = {
    # Flavonols
    "quercetin":                          "Quercetin",
    "kaempferol":                         "Kaempferol",
    "myricetin":                          "Myricetin",
    "fisetin":                            "Fisetin",
    "rutin":                              "Rutin",
    "isorhamnetin":                       "Isorhamnetin",
    "morin":                              "Morin",
    # Flavones
    "luteolin":                           "Luteolin",
    "apigenin":                           "Apigenin",
    "baicalein":                          "Baicalein",
    "chrysin":                            "Chrysin",
    "acacetin":                           "Acacetin",
    "nobiletin":                          "Nobiletin",
    "tangeretin":                         "Tangeretin",
    # Flavanones
    "naringenin":                         "Naringenin",
    "hesperetin":                         "Hesperetin",
    "naringin":                           "Naringin",
    "hesperidin":                         "Hesperidin",
    "eriodictyol":                        "Eriodictyol",
    # Catechins
    "epigallocatechin gallate":           "Epigallocatechin gallate",
    "(-)-epigallocatechin gallate":       "Epigallocatechin gallate",
    "egcg":                               "Epigallocatechin gallate",
    "epicatechin":                        "Epicatechin",
    "(-)-epicatechin":                    "Epicatechin",
    "catechin":                           "Catechin",
    "(+)-catechin":                       "Catechin",
    "epigallocatechin":                   "Epigallocatechin",
    "procyanidin b2":                     "Procyanidin B2",
    # Isoflavones
    "genistein":                          "Genistein",
    "daidzein":                           "Daidzein",
    "formononetin":                       "Formononetin",
    "biochanin a":                        "Biochanin A",
    # Anthocyanins
    "cyanidin":                           "Cyanidin",
    "delphinidin":                        "Delphinidin",
    "malvidin":                           "Malvidin",
    "pelargonidin":                       "Pelargonidin",
    # Stilbenoids
    "resveratrol":                        "Resveratrol",
    "trans-resveratrol":                  "Resveratrol",
    "pterostilbene":                      "Pterostilbene",
    # Curcuminoids
    "curcumin":                           "Curcumin",
    "demethoxycurcumin":                  "Demethoxycurcumin",
    # Phenolic / hydroxycinnamic acids
    "ellagic acid":                       "Ellagic acid",
    "gallic acid":                        "Gallic acid",
    "caffeic acid":                       "Caffeic acid",
    "ferulic acid":                       "Ferulic acid",
    "chlorogenic acid":                   "Chlorogenic acid",
    "rosmarinic acid":                    "Rosmarinic acid",
    "p-coumaric acid":                    "p-Coumaric acid",
    "4-coumaric acid":                    "p-Coumaric acid",
    "sinapic acid":                       "Sinapic acid",
    "protocatechuic acid":                "Protocatechuic acid",
    "syringic acid":                      "Syringic acid",
    "hydroxytyrosol":                     "Hydroxytyrosol",
    "oleuropein":                         "Oleuropein",
    "tyrosol":                            "Tyrosol",
    "vanillin":                           "Vanillin",
    # Carotenoids
    "beta-carotene":                      "Beta-carotene",
    "beta-carotene [c00523]":            "Beta-carotene",
    "lycopene":                           "Lycopene",
    "lutein":                             "Lutein",
    "zeaxanthin":                         "Zeaxanthin",
    "astaxanthin":                        "Astaxanthin",
    "alpha-carotene":                     "Alpha-carotene",
    "beta-cryptoxanthin":                 "Beta-cryptoxanthin",
    "fucoxanthin":                        "Fucoxanthin",
    "crocin":                             "Crocin",
    "crocetin":                           "Crocetin",
    # Organosulfur
    "allicin":                            "Allicin",
    "sulforaphane":                       "Sulforaphane",
    "diallyl disulfide":                  "Diallyl disulfide",
    "indole-3-carbinol":                  "Indole-3-carbinol",
    "diindolylmethane":                   "Diindolylmethane",
    # Omega fatty acids
    "docosahexaenoic acid":               "Docosahexaenoic acid",
    "dha":                                "Docosahexaenoic acid",
    "(4z,7z,10z,13z,16z,19z)-docosahexaenoic acid": "Docosahexaenoic acid",
    "eicosapentaenoic acid":              "Eicosapentaenoic acid",
    "epa":                                "Eicosapentaenoic acid",
    "alpha-linolenic acid":               "Alpha-linolenic acid",
    "gamma-linolenic acid":               "Gamma-linolenic acid",
    "conjugated linoleic acid":           "Conjugated linoleic acid",
    "oleic acid":                         "Oleic acid",
    # Terpenoids / phenylpropanoids
    "gingerol":                           "Gingerol",
    "[6]-gingerol":                       "Gingerol",
    "capsaicin":                          "Capsaicin",
    "piperine":                           "Piperine",
    "thymoquinone":                       "Thymoquinone",
    "carvacrol":                          "Carvacrol",
    "thymol":                             "Thymol",
    "limonene":                           "Limonene",
    "d-limonene":                         "Limonene",
    "ursolic acid":                       "Ursolic acid",
    "oleanolic acid":                     "Oleanolic acid",
    "betulinic acid":                     "Betulinic acid",
    "andrographolide":                    "Andrographolide",
    # Alkaloids
    "berberine":                          "Berberine",
    "caffeine":                           "Caffeine",
    "theophylline":                       "Theophylline",
    "theobromine":                        "Theobromine",
    # Vitamins
    "cholecalciferol":                    "Vitamin D3",
    "vitamin d3":                         "Vitamin D3",
    "alpha-tocopherol":                   "Alpha-tocopherol",
    "(r)-alpha-tocopherol":               "Alpha-tocopherol",
    "vitamin e":                          "Alpha-tocopherol",
    "ascorbic acid":                      "Ascorbic acid",
    "l-ascorbic acid":                    "Ascorbic acid",
    "vitamin c":                          "Ascorbic acid",
    # Coenzymes / other
    "coenzyme q10":                       "Coenzyme Q10",
    "ubiquinone-10":                      "Coenzyme Q10",
    "ubiquinol-10":                       "Coenzyme Q10",
    "alpha-lipoic acid":                  "Alpha-lipoic acid",
    "lipoic acid":                        "Alpha-lipoic acid",
    "melatonin":                          "Melatonin",
    "spermidine":                         "Spermidine",
    "ergothioneine":                      "Ergothioneine",
    # Lignans
    "secoisolariciresinol":               "Secoisolariciresinol",
    "matairesinol":                       "Matairesinol",
    # Flavonolignans / other
    "silymarin":                          "Silymarin",
    "honokiol":                           "Honokiol",
    "urolithin a":                        "Urolithin A",
    "taxifolin":                          "Taxifolin",
    "dihydroquercetin":                   "Taxifolin",
}


# ── KEGG helpers ──────────────────────────────────────────────────────────────

def _is_metabolite(kegg_cid: str) -> bool:
    """True for C-prefix metabolites; False for D-prefix drugs."""
    bare = kegg_cid.upper().replace("CPD:", "").strip()
    return bare.startswith("C")


def _parse_tsv_col1(text: str) -> list[str]:
    """Extract column 1 (0-indexed) from KEGG tab-separated link output."""
    out = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) > 1 and parts[1].strip():
            out.append(parts[1].strip())
    return out


async def _fetch_compound_names_batch(
    client: httpx.AsyncClient,
    kegg_ids: list[str],
    rate: RateLimiter,
    batch_size: int = 10,
) -> dict[str, str]:
    """Return {kegg_id: primary_name} for a list of C-prefix KEGG IDs."""
    id_to_name: dict[str, str] = {}
    for i in range(0, len(kegg_ids), batch_size):
        batch = kegg_ids[i : i + batch_size]
        joined = "+".join(batch)
        await rate.wait()
        try:
            resp = await client.get(f"{KEGG_BASE}/get/{joined}")
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            text = resp.text
        except Exception as exc:
            logger.warning("KEGG /get failed for batch [%s…]: %s", joined[:40], exc)
            continue

        for record in text.split("///"):
            record = record.strip()
            if not record:
                continue
            entry_m = re.search(r"^ENTRY\s+(C\d+)", record, re.MULTILINE)
            if not entry_m:
                continue
            cid = f"cpd:{entry_m.group(1)}"
            name_m = re.search(r"^NAME\s+(.+)$", record, re.MULTILINE)
            if name_m:
                # NAME may be semicolon-separated synonyms; take the first
                primary = name_m.group(1).split(";")[0].strip()
                if primary:
                    id_to_name[cid] = primary

    return id_to_name


def _match_to_db_compound(kegg_name: str, db_compound_names: set[str]) -> str | None:
    """Try to match a KEGG compound name to one in our database.

    Resolution order:
    1. Explicit alias map (handles alternate KEGG names / systematic names)
    2. Case-insensitive exact match against DB compound names
    3. Case-insensitive prefix match (DB name starts with KEGG name or vice versa)
    """
    lower = kegg_name.lower().strip()

    # 1. Alias map
    alias = KEGG_NAME_ALIASES.get(lower)
    if alias and alias in db_compound_names:
        return alias

    # 2. Case-insensitive exact match
    for db_name in db_compound_names:
        if db_name.lower() == lower:
            return db_name

    # 3. Prefix match (at least 8 chars to avoid false positives)
    if len(lower) >= 8:
        for db_name in db_compound_names:
            db_lower = db_name.lower()
            if db_lower.startswith(lower) or lower.startswith(db_lower):
                return db_name

    return None


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(dry_run: bool = False, batch_size: int = 50) -> None:
    from app.api.mygene_converter import get_entrez_id
    from app.database import get_supabase

    checkpoint = load_checkpoint(CHECKPOINT_NAME)
    processed_symbols: set[str] = set(checkpoint.get("processed", []))

    logger.info(
        "Starting KEGG compound interaction population%s. Processed: %d",
        " [DRY RUN]" if dry_run else "",
        len(processed_symbols),
    )

    db_client = await get_supabase()

    # Load genes
    gene_resp = await db_client.table("genes").select("id, gene_symbol").execute()
    genes: list[dict[str, Any]] = gene_resp.data or []
    logger.info("Loaded %d gene(s) from database.", len(genes))

    # Load our compound name→UUID map
    cmpd_resp = await db_client.table("compounds").select("id, compound_name").execute()
    compound_map: dict[str, str] = {
        r["compound_name"]: r["id"] for r in (cmpd_resp.data or [])
    }
    db_compound_names: set[str] = set(compound_map.keys())
    logger.info("Loaded %d compound(s) from database.", len(compound_map))

    # Load existing (compound_id, gene_id) pairs for dedup
    existing_resp = await db_client.table("compound_gene_interactions") \
        .select("compound_id, gene_id").execute()
    db_pairs: set[tuple[str, str]] = {
        (r["compound_id"], r["gene_id"]) for r in (existing_resp.data or [])
    }
    staged_pairs: set[tuple[str, str]] = set()
    logger.info("Loaded %d existing interaction pair(s).", len(db_pairs))

    interaction_records: list[dict[str, Any]] = []

    symbols_to_process = [
        g for g in genes
        if g["gene_symbol"] not in processed_symbols
    ]
    logger.info("%d gene(s) remaining to process.", len(symbols_to_process))

    kegg_rate = RateLimiter(calls_per_second=1.0)

    async with httpx.AsyncClient(timeout=30.0) as client:
        for i, gene_row in enumerate(symbols_to_process):
            symbol    = gene_row["gene_symbol"]
            gene_uuid = gene_row["id"]

            # Resolve Entrez ID via mygene_converter (symbol-based)
            await kegg_rate.wait()
            entrez_id = await get_entrez_id(gene_symbol=symbol, gene_id=None)
            if not entrez_id:
                logger.warning("[%d/%d] No Entrez ID for %s — skipping",
                               i + 1, len(symbols_to_process), symbol)
                checkpoint["failed"].append(symbol)
                save_checkpoint(CHECKPOINT_NAME, checkpoint)
                continue

            kegg_gene_id = f"hsa:{entrez_id}"
            logger.info("[%d/%d] Fetching KEGG compounds for %s (%s)",
                        i + 1, len(symbols_to_process), symbol, kegg_gene_id)

            # ── /link/compound/{gene} ─────────────────────────────────────────
            try:
                await kegg_rate.wait()
                resp = await client.get(f"{KEGG_BASE}/link/compound/{kegg_gene_id}")
                if resp.status_code == 404 or not resp.text.strip():
                    logger.debug("  No KEGG compounds for %s", symbol)
                    checkpoint["processed"].append(symbol)
                    continue
                resp.raise_for_status()
                link_text = resp.text
            except Exception as exc:
                logger.warning("  KEGG /link/compound failed for %s: %s", symbol, exc)
                checkpoint["failed"].append(symbol)
                save_checkpoint(CHECKPOINT_NAME, checkpoint)
                continue

            all_ids   = _parse_tsv_col1(link_text)
            seen: set[str] = set()
            unique_metabolite_ids = [
                cid for cid in all_ids
                if _is_metabolite(cid) and cid not in seen and not seen.add(cid)  # type: ignore[func-returns-value]
            ]
            logger.info("  → %d metabolite KEGG ID(s) for %s (from %d total)",
                        len(unique_metabolite_ids), symbol, len(all_ids))

            if not unique_metabolite_ids:
                checkpoint["processed"].append(symbol)
                continue

            # ── Batch-fetch compound names ────────────────────────────────────
            id_to_name = await _fetch_compound_names_batch(
                client, unique_metabolite_ids, kegg_rate
            )

            # ── Match names to our DB compounds ──────────────────────────────
            matched = 0
            for kegg_cid, kegg_name in id_to_name.items():
                db_name = _match_to_db_compound(kegg_name, db_compound_names)
                if not db_name:
                    logger.debug("    No DB match for KEGG compound '%s'", kegg_name)
                    continue

                compound_uuid = compound_map[db_name]
                pair = (compound_uuid, gene_uuid)
                if pair in db_pairs or pair in staged_pairs:
                    logger.debug("    Already exists: %s × %s", db_name, symbol)
                    continue

                interaction_records.append({
                    "compound_id":       compound_uuid,
                    "gene_id":           gene_uuid,
                    "interaction_type":  "modulator",
                    "bioactivity_score": CONFIDENCE_SCORE,
                    "evidence_type":     "database",
                    "source":            "kegg",
                })
                staged_pairs.add(pair)
                matched += 1
                logger.debug("    Matched: '%s' → '%s' × %s", kegg_name, db_name, symbol)

            logger.info("  Matched %d compound(s) for %s. Total staged: %d",
                        matched, symbol, len(interaction_records))

            checkpoint["processed"].append(symbol)
            if (i + 1) % batch_size == 0:
                save_checkpoint(CHECKPOINT_NAME, checkpoint)
                logger.info("Checkpoint saved at gene %d/%d. Staged: %d interactions",
                            i + 1, len(symbols_to_process), len(interaction_records))

    save_checkpoint(CHECKPOINT_NAME, checkpoint)
    logger.info(
        "Staged %d compound_gene_interaction row(s) across %d gene(s).",
        len(interaction_records), len(symbols_to_process),
    )

    # ── Coverage report ───────────────────────────────────────────────────────
    covered_compounds: set[str] = set()
    covered_genes:     set[str] = set()
    for rec in interaction_records:
        covered_compounds.add(rec["compound_id"])
        covered_genes.add(rec["gene_id"])
    logger.info(
        "Coverage: %d/%d compounds, %d/%d genes have at least one new interaction.",
        len(covered_compounds), len(compound_map),
        len(covered_genes), len(genes),
    )

    if dry_run:
        logger.info("[DRY RUN] Would insert %d interaction row(s).", len(interaction_records))
        logger.info("Done. Failed: %d gene(s).", len(checkpoint.get("failed", [])))
        return

    if interaction_records:
        inserted = await batch_insert(
            db_client, "compound_gene_interactions", interaction_records, logger=logger
        )
        logger.info("Inserted %d compound_gene_interaction row(s).", inserted)
    else:
        logger.warning("No new interactions to insert.")

    logger.info("Done. Failed: %d gene(s).", len(checkpoint.get("failed", [])))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Expand compound_gene_interactions from KEGG."
    )
    parser.add_argument("--dry-run",    action="store_true",
                        help="Preview without inserting")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="Checkpoint save frequency in genes (default: 50)")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run, batch_size=args.batch_size))
