"""Upsert supabase/seeds/phytochemical_sources_expanded.sql into Supabase.

PostgREST cannot execute raw SQL, so the generated INSERT is parsed back into
rows and replayed with .upsert(ignore_duplicates=True) — which mirrors the
file's ON CONFLICT DO NOTHING, leaving the hand-curated rows alone.

Usage:
    python scripts/seed_phytochemical_sources_expanded.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

ROOT = Path(__file__).resolve().parent.parent
SQL_FILE = ROOT / "supabase" / "seeds" / "phytochemical_sources_expanded.sql"
TABLE = "phytochemical_sources"
BATCH_SIZE = 100

# ('Name', ARRAY['a','b'], ARRAY['c'], 'Class')
ROW_RE = re.compile(
    r"\(\s*'((?:[^']|'')*)'"          # phytochemical_name
    r"\s*,\s*ARRAY\[([^\]]*)\]"       # fruit_vegetables
    r"\s*,\s*ARRAY\[([^\]]*)\]"       # primary_sources
    r"\s*,\s*'((?:[^']|'')*)'\s*\)",  # chemical_class
    re.MULTILINE,
)

ITEM_RE = re.compile(r"'((?:[^']|'')*)'")


def unquote(value: str) -> str:
    return value.replace("''", "'").strip()


def split_array(body: str) -> list[str]:
    return [unquote(m.group(1)) for m in ITEM_RE.finditer(body)]


def parse_rows(text: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for m in ROW_RE.finditer(text):
        name = unquote(m.group(1))
        if not name:
            continue
        rows.append(
            {
                "phytochemical_name": name,
                "fruit_vegetables": split_array(m.group(2)),
                "primary_sources": split_array(m.group(3)),
                "chemical_class": unquote(m.group(4)) or None,
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not SQL_FILE.exists():
        print(f"ERROR: {SQL_FILE} not found — run build_phytochemical_sources.py first.")
        return 1

    rows = parse_rows(SQL_FILE.read_text(encoding="utf-8"))
    print(f"Parsed {len(rows):,} rows from {SQL_FILE.name}")
    if not rows:
        print("ERROR: parsed no rows — the file format may have changed.")
        return 1

    if args.dry_run:
        for r in rows[:5]:
            print("  ", r)
        print("(dry run — nothing written)")
        return 0

    load_dotenv(ROOT / ".env")
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL and SUPABASE_KEY must be set in .env")
        return 1

    client = create_client(url, key)

    before = client.table(TABLE).select("id", count="exact").limit(1).execute().count or 0
    print(f"Rows before: {before:,}")

    start = time.time()
    written = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        for attempt in range(1, 4):
            try:
                # ignore_duplicates mirrors ON CONFLICT DO NOTHING: curated rows win.
                client.table(TABLE).upsert(
                    batch, on_conflict="phytochemical_name", ignore_duplicates=True
                ).execute()
                break
            except Exception as exc:
                if attempt == 3:
                    raise
                print(f"  WARN: batch failed ({exc.__class__.__name__}), retrying in {attempt}s")
                time.sleep(attempt)
        written += len(batch)
        print(f"  ... {written:,}/{len(rows):,} sent | {time.time() - start:.1f}s", flush=True)

    after = client.table(TABLE).select("id", count="exact").limit(1).execute().count or 0
    print()
    print(f"Done in {time.time() - start:.1f}s")
    print(f"  Rows before : {before:,}")
    print(f"  Rows after  : {after:,}")
    print(f"  Rows added  : {after - before:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
