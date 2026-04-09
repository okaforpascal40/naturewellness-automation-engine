"""Diagnostic script — queries DB state and tests sample inserts for each junction table.

Usage:
    python data_population/debug_population.py
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from utils import setup_logging

logger = setup_logging("debug_population")


async def query_table_count(db_client: Any, table: str) -> int:
    resp = await db_client.table(table).select("id", count="exact").execute()
    return resp.count or len(resp.data or [])


async def main() -> None:
    from app.database import get_supabase

    db = await get_supabase()

    # ── 1. Current table counts ───────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("CURRENT DATABASE STATE")
    logger.info("=" * 60)

    tables = [
        "genes", "pathways", "compounds", "foods",
        "gene_pathway_associations",
        "compound_gene_interactions",
        "food_compound_associations",
    ]
    counts: dict[str, int] = {}
    for table in tables:
        try:
            resp = await db.table(table).select("id", count="exact").execute()
            n = resp.count if resp.count is not None else len(resp.data or [])
            counts[table] = n
            logger.info("  %-38s %d rows", table, n)
        except Exception as exc:
            logger.error("  %-38s ERROR: %s", table, exc)

    # ── 2. Sample data checks ─────────────────────────────────────────────────
    logger.info("")
    logger.info("SAMPLE DATA")
    logger.info("=" * 60)

    for table, col in [
        ("genes",     "gene_symbol"),
        ("pathways",  "pathway_id"),
        ("compounds", "compound_name"),
        ("foods",     "name"),
    ]:
        try:
            resp = await db.table(table).select(f"id, {col}").limit(3).execute()
            rows = resp.data or []
            logger.info("  %s sample: %s", table, [r.get(col, "?") for r in rows])
        except Exception as exc:
            logger.error("  %s sample FAILED: %s", table, exc)

    # ── 3. Sample insert tests ─────────────────────────────────────────────────
    logger.info("")
    logger.info("SAMPLE INSERT TESTS (10 rows × 3 tables)")
    logger.info("=" * 60)

    # Fetch real UUIDs for test inserts
    genes_resp    = await db.table("genes")   .select("id, gene_symbol").limit(5).execute()
    pathways_resp = await db.table("pathways").select("id, pathway_id").limit(5).execute()
    cmpds_resp    = await db.table("compounds").select("id, compound_name").limit(5).execute()
    foods_resp    = await db.table("foods")   .select("id, name").limit(5).execute()

    genes    = genes_resp.data    or []
    pathways = pathways_resp.data or []
    cmpds    = cmpds_resp.data    or []
    foods    = foods_resp.data    or []

    logger.info(
        "Fetched: %d genes, %d pathways, %d compounds, %d foods for test inserts.",
        len(genes), len(pathways), len(cmpds), len(foods),
    )

    # Test gene_pathway_associations (up to 10 rows)
    if genes and pathways:
        test_gpa = []
        for i in range(min(3, len(genes))):
            for j in range(min(3, len(pathways))):
                test_gpa.append({
                    "gene_id":          genes[i]["id"],
                    "pathway_id":       pathways[j]["id"],
                    "confidence_score": 0.99,
                })
        logger.info("Attempting to insert %d test gene_pathway_associations …", len(test_gpa))
        try:
            result = await db.table("gene_pathway_associations").insert(test_gpa).execute()
            inserted = len(result.data or [])
            logger.info("  ✓ Inserted %d gene_pathway_association test rows.", inserted)
            # Clean up test rows
            for r in (result.data or []):
                await db.table("gene_pathway_associations").delete().eq("id", r["id"]).execute()
            logger.info("  Cleaned up %d test rows.", inserted)
        except Exception as exc:
            logger.error("  ✗ gene_pathway_associations insert FAILED: %s", exc)
    else:
        logger.warning("  Skipping gene_pathway_associations test — missing genes or pathways.")

    # Test compound_gene_interactions (up to 9 rows)
    if cmpds and genes:
        test_cgi = []
        for i in range(min(3, len(cmpds))):
            for j in range(min(3, len(genes))):
                test_cgi.append({
                    "compound_id":       cmpds[i]["id"],
                    "gene_id":           genes[j]["id"],
                    "interaction_type":  "modulator",
                    "bioactivity_score": 0.99,
                    "evidence_type":     "test",
                    "source":            "debug_script",
                })
        logger.info("Attempting to insert %d test compound_gene_interactions …", len(test_cgi))
        try:
            result = await db.table("compound_gene_interactions").insert(test_cgi).execute()
            inserted = len(result.data or [])
            logger.info("  ✓ Inserted %d compound_gene_interaction test rows.", inserted)
            for r in (result.data or []):
                await db.table("compound_gene_interactions").delete().eq("id", r["id"]).execute()
            logger.info("  Cleaned up %d test rows.", inserted)
        except Exception as exc:
            logger.error("  ✗ compound_gene_interactions insert FAILED: %s", exc)
    else:
        logger.warning("  Skipping compound_gene_interactions test — missing compounds or genes.")

    # Test food_compound_associations (up to 9 rows)
    if foods and cmpds:
        test_fca = []
        for i in range(min(3, len(foods))):
            for j in range(min(3, len(cmpds))):
                test_fca.append({
                    "food_id":                    foods[i]["id"],
                    "compound_id":                cmpds[j]["id"],
                    "concentration_mg_per_100g":  1.0,
                    "concentration_category":     "low",
                    "source":                     "debug_script",
                })
        logger.info("Attempting to insert %d test food_compound_associations …", len(test_fca))
        try:
            result = await db.table("food_compound_associations").insert(test_fca).execute()
            inserted = len(result.data or [])
            logger.info("  ✓ Inserted %d food_compound_association test rows.", inserted)
            for r in (result.data or []):
                await db.table("food_compound_associations").delete().eq("id", r["id"]).execute()
            logger.info("  Cleaned up %d test rows.", inserted)
        except Exception as exc:
            logger.error("  ✗ food_compound_associations insert FAILED: %s", exc)
    else:
        logger.warning("  Skipping food_compound_associations test — missing foods or compounds.")

    # ── 4. Schema probe — check if unique constraints could block inserts ──────
    logger.info("")
    logger.info("DUPLICATE CONSTRAINT PROBE")
    logger.info("=" * 60)

    if genes and pathways:
        # Check if existing rows block re-insert
        existing_resp = await db.table("gene_pathway_associations") \
            .select("gene_id, pathway_id") \
            .limit(1).execute()
        if existing_resp.data:
            dup_row = existing_resp.data[0]
            logger.info("Testing duplicate insert for gene_pathway_associations …")
            try:
                await db.table("gene_pathway_associations").insert({
                    "gene_id":          dup_row["gene_id"],
                    "pathway_id":       dup_row["pathway_id"],
                    "confidence_score": 0.50,
                }).execute()
                logger.info("  ✓ Duplicate insert SUCCEEDED (no unique constraint)")
            except Exception as exc:
                logger.info("  ✗ Duplicate insert BLOCKED: %s", exc)
                logger.info("  → This means existing rows prevent re-insertion via non-upsert.")

    logger.info("")
    logger.info("DIAGNOSIS COMPLETE")
    logger.info("=" * 60)
    logger.info("Summary:")
    for table, n in counts.items():
        logger.info("  %-38s %d", table, n)


if __name__ == "__main__":
    asyncio.run(main())
