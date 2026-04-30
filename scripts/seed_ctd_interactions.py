"""Upsert filtered CTD phytochemical-gene interactions into Supabase.

Reads scripts/ctd_phytochemical_interactions_filtered.tsv (produced by
filter_ctd_bulk.py) and bulk-upserts into ctd_phytochemical_gene_interactions.

Usage:
    python scripts/seed_ctd_interactions.py
"""
from __future__ import annotations

import csv
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

ROOT = Path(__file__).resolve().parent.parent
INPUT_PATH = ROOT / "scripts" / "ctd_phytochemical_interactions_filtered.tsv"
TABLE = "ctd_phytochemical_gene_interactions"
BATCH_SIZE = 500


def parse_pmids(value: str) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in value.split("|") if p.strip()]


def parse_actions(value: str) -> list[str]:
    if not value:
        return []
    return [a.strip() for a in value.split("|") if a.strip()]


def row_to_record(row: dict[str, str]) -> dict[str, object]:
    pmids = parse_pmids(row.get("PubMedIDs", ""))
    return {
        "chemical_name": (row.get("ChemicalName") or "").strip(),
        "chemical_id": (row.get("ChemicalID") or "").strip() or None,
        "gene_symbol": (row.get("GeneSymbol") or "").strip(),
        "gene_id": (row.get("GeneID") or "").strip() or None,
        "interaction_actions": parse_actions(row.get("InteractionActions", "")),
        "pubmed_ids": pmids,
        "publication_count": len(pmids),
    }


def main() -> int:
    load_dotenv(ROOT / ".env")
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL and SUPABASE_KEY must be set in .env")
        return 1

    if not INPUT_PATH.exists():
        print(f"ERROR: input file not found: {INPUT_PATH}")
        print("Run scripts/filter_ctd_bulk.py first.")
        return 1

    client = create_client(url, key)
    print(f"Seeding from: {INPUT_PATH}")
    print(f"Target table: {TABLE}")
    print(f"Batch size  : {BATCH_SIZE}")
    print()

    # Clean reseed — delete any existing rows so re-runs don't duplicate.
    print("Clearing existing rows...")
    try:
        client.table(TABLE).delete().neq(
            "chemical_name", "__sentinel_does_not_exist__"
        ).execute()
        print("  cleared.")
    except Exception as exc:
        print(f"  WARN: clear step failed: {exc} (continuing — table may be empty)")
    print()

    start = time.time()
    batch: list[dict[str, object]] = []
    total_read = 0
    total_inserted = 0
    last_log = start

    with INPUT_PATH.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            record = row_to_record(row)
            if not record["chemical_name"] or not record["gene_symbol"]:
                continue
            batch.append(record)
            total_read += 1

            if len(batch) >= BATCH_SIZE:
                _flush(client, batch)
                total_inserted += len(batch)
                batch.clear()

                now = time.time()
                if now - last_log >= 3.0:
                    print(
                        f"  ... {total_inserted:,} inserted | {now - start:.1f}s",
                        flush=True,
                    )
                    last_log = now

        if batch:
            _flush(client, batch)
            total_inserted += len(batch)

    elapsed = time.time() - start
    print()
    print(f"Done in {elapsed:.1f}s")
    print(f"  Rows read     : {total_read:,}")
    print(f"  Rows inserted : {total_inserted:,}")

    # Verify via count
    try:
        resp = client.table(TABLE).select("id", count="exact").limit(1).execute()
        print(f"  Table total   : {resp.count:,}")
    except Exception as exc:
        print(f"  (count check skipped: {exc})")
    return 0


def _flush(client, batch: list[dict[str, object]]) -> None:
    client.table(TABLE).insert(batch).execute()


if __name__ == "__main__":
    sys.exit(main())
