"""Step 2 — Populate pathways and gene_pathway_associations from KEGG.

Reads gene symbols from the Supabase genes table and uses the MyGene.info
symbol→Entrez conversion (via app/api/mygene_converter.py) to resolve KEGG
IDs. No Ensembl ID extraction required — the converter handles everything.

Usage:
    python data_population/populate_kegg_pathways.py [--dry-run] [--batch-size 50]

Writes progress to checkpoints/kegg_pathways.json so interrupted runs resume.
"""
from __future__ import annotations

import argparse
import asyncio
import uuid as _uuid
from typing import Any

from utils import (
    RateLimiter,
    batch_insert,
    get_existing_values,
    load_checkpoint,
    save_checkpoint,
    setup_logging,
)

CHECKPOINT_NAME  = "kegg_pathways"
CONFIDENCE_SCORE = 0.80   # KEGG is highly curated

logger = setup_logging("kegg_pathways")


async def main(dry_run: bool = False, batch_size: int = 50) -> None:
    from app.api.kegg import get_pathways_for_gene
    from app.database import get_supabase

    checkpoint = load_checkpoint(CHECKPOINT_NAME)
    processed_symbols: set[str] = set(checkpoint.get("processed", []))
    failed_symbols:    set[str] = set(checkpoint.get("failed",    []))

    logger.info(
        "Starting KEGG pathway population%s. Processed: %d  Failed: %d",
        " [DRY RUN]" if dry_run else "",
        len(processed_symbols),
        len(failed_symbols),
    )

    db_client = await get_supabase()

    # Only need symbol + Supabase UUID — Entrez resolution done by mygene_converter
    gene_resp = await db_client.table("genes").select("id, gene_symbol").execute()
    genes: list[dict[str, Any]] = gene_resp.data or []
    logger.info("Loaded %d gene(s) from database.", len(genes))

    # Pre-load existing data to deduplicate at insert time
    existing_pathway_ids = await get_existing_values(db_client, "pathways", "pathway_id")

    # Load existing (gene_id, pathway_id) pairs — used only for final dedup check
    assoc_resp = await db_client.table("gene_pathway_associations") \
        .select("gene_id, pathway_id").execute()
    db_assoc_pairs: set[tuple[str, str]] = {
        (r["gene_id"], r["pathway_id"]) for r in (assoc_resp.data or [])
    }
    # Separate set for within-run dedup (prevents staging the same pair twice)
    staged_pairs: set[tuple[str, str]] = set()
    logger.info(
        "DB state: %d existing pathway(s), %d existing association pair(s).",
        len(existing_pathway_ids), len(db_assoc_pairs),
    )

    pathway_records:  list[dict[str, Any]] = []
    assoc_records:    list[dict[str, Any]] = []
    pathway_uuid_map: dict[str, str]       = {}   # kegg_pathway_id → new or deferred UUID

    kegg_rate = RateLimiter(calls_per_second=1.0)   # KEGG: ≤1 req/s recommended

    symbols_to_process = [
        g for g in genes
        if g["gene_symbol"] not in processed_symbols
    ]
    logger.info("%d gene(s) remaining to process.", len(symbols_to_process))

    for i, gene_row in enumerate(symbols_to_process):
        symbol    = gene_row["gene_symbol"]
        gene_uuid = gene_row["id"]

        await kegg_rate.wait()
        logger.info(
            "[%d/%d] Fetching KEGG pathways for %s",
            i + 1, len(symbols_to_process), symbol,
        )

        try:
            # gene_id=None → mygene_converter resolves via symbol lookup
            pathways = await get_pathways_for_gene(gene_symbol=symbol, gene_id=None)
        except Exception as exc:
            logger.warning("KEGG pathway fetch failed for %s: %s", symbol, exc)
            checkpoint["failed"].append(symbol)
            save_checkpoint(CHECKPOINT_NAME, checkpoint)
            continue

        logger.info("  → Found %d pathway(s) for %s", len(pathways), symbol)

        for pw in pathways:
            kegg_pid = pw.pathway_id   # e.g. "path:hsa04110"
            pw_name  = pw.pathway_name

            # Stage new pathway record if not already in DB and not seen this run
            if kegg_pid not in existing_pathway_ids and kegg_pid not in pathway_uuid_map:
                new_uuid = str(_uuid.uuid4())
                pathway_uuid_map[kegg_pid] = new_uuid
                pathway_records.append({
                    "id":           new_uuid,
                    "pathway_id":   kegg_pid,
                    "pathway_name": pw_name,
                    "description":  f"KEGG pathway {kegg_pid}",
                })
                logger.debug("    Staged NEW pathway: %s (%s)", kegg_pid, pw_name)
            elif kegg_pid in existing_pathway_ids:
                # Already in DB — defer UUID resolution until after insert
                pathway_uuid_map.setdefault(kegg_pid, "__db__")
                logger.debug("    Pathway %s already in DB — deferring UUID", kegg_pid)

            pw_uuid = pathway_uuid_map.get(kegg_pid, "")

            if pw_uuid and pw_uuid != "__db__":
                pair = (gene_uuid, pw_uuid)
                if pair not in db_assoc_pairs and pair not in staged_pairs:
                    assoc_records.append({
                        "gene_id":          gene_uuid,
                        "pathway_id":       pw_uuid,
                        "confidence_score": CONFIDENCE_SCORE,
                    })
                    staged_pairs.add(pair)
                    logger.debug("    Staged assoc: %s → %s", symbol, kegg_pid)
                else:
                    logger.debug("    Already exists: %s → %s", symbol, kegg_pid)
            elif pw_uuid == "__db__":
                # Embed kegg_pid so we can resolve the UUID after the insert;
                # pair-check happens again post-resolution before final insert
                assoc_records.append({
                    "gene_id":          gene_uuid,
                    "pathway_id":       "__db__:" + kegg_pid,
                    "confidence_score": CONFIDENCE_SCORE,
                })
                logger.debug("    Staged DEFERRED assoc: %s → %s", symbol, kegg_pid)
            else:
                logger.warning(
                    "    No UUID for pathway %s — skipping assoc for %s", kegg_pid, symbol
                )

        logger.info(
            "  Running totals: %d pathway(s) staged, %d assoc(s) staged",
            len(pathway_records), len(assoc_records),
        )

        checkpoint["processed"].append(symbol)
        if (i + 1) % batch_size == 0:
            save_checkpoint(CHECKPOINT_NAME, checkpoint)
            logger.info(
                "Checkpoint saved. Staged: %d pathways, %d associations",
                len(pathway_records), len(assoc_records),
            )

    save_checkpoint(CHECKPOINT_NAME, checkpoint)
    logger.info(
        "Staged %d new pathway row(s) and %d association row(s).",
        len(pathway_records), len(assoc_records),
    )

    if dry_run:
        logger.info(
            "[DRY RUN] Would insert %d pathways and %d associations.",
            len(pathway_records), len(assoc_records),
        )
        logger.info("Done. Failed: %d gene(s).", len(checkpoint.get("failed", [])))
        return

    # ── Insert new pathway rows ───────────────────────────────────────────────
    if pathway_records:
        inserted_pw = await batch_insert(db_client, "pathways", pathway_records, logger=logger)
        logger.info("Inserted %d pathway row(s).", inserted_pw)

    # ── Resolve deferred association UUIDs ────────────────────────────────────
    deferred = [a for a in assoc_records if str(a["pathway_id"]).startswith("__db__:")]
    if deferred:
        logger.info("Resolving %d deferred association(s) …", len(deferred))
        pw_resp   = await db_client.table("pathways").select("id, pathway_id").execute()
        db_pw_map = {r["pathway_id"]: r["id"] for r in (pw_resp.data or [])}
        logger.info("Loaded %d pathway UUID(s) from DB.", len(db_pw_map))

        def _resolve(a: dict) -> dict:
            pid = a["pathway_id"]
            if str(pid).startswith("__db__:"):
                kegg_pid = pid[len("__db__:"):]
                resolved = db_pw_map.get(kegg_pid, "")
                if not resolved:
                    logger.warning("Could not resolve DB UUID for pathway %s", kegg_pid)
                return {**a, "pathway_id": resolved}
            return a

        assoc_records = [_resolve(a) for a in assoc_records]

    valid_assocs = [
        a for a in assoc_records
        if a["pathway_id"] not in ("__db__", "", None)
        and (a["gene_id"], a["pathway_id"]) not in db_assoc_pairs
    ]
    dropped = len(assoc_records) - len(valid_assocs)
    if dropped:
        logger.warning("Dropped %d unresolved association(s).", dropped)

    if valid_assocs:
        inserted_assoc = await batch_insert(
            db_client, "gene_pathway_associations", valid_assocs, logger=logger
        )
        logger.info("Inserted %d gene_pathway_association row(s).", inserted_assoc)
    else:
        logger.warning("No valid associations to insert.")

    logger.info("Done. Failed: %d gene(s).", len(checkpoint.get("failed", [])))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populate pathways from KEGG.")
    parser.add_argument("--dry-run",    action="store_true")
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run, batch_size=args.batch_size))
