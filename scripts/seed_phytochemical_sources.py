"""Parse supabase/seeds/phytochemical_sources.sql and upsert into Supabase.

We can't execute raw SQL via the service-role API key (PostgREST is CRUD-only),
so this script extracts the INSERT rows and replays them via .upsert().

Usage:
    python scripts/seed_phytochemical_sources.py
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

ROOT = Path(__file__).resolve().parent.parent
SQL_FILE = ROOT / "supabase" / "seeds" / "phytochemical_sources.sql"
TABLE = "phytochemical_sources"
BATCH_SIZE = 100

# Match: ('Name', ARRAY['a','b',...], ARRAY['c','d',...], 'Class')
# Trailing comma or semicolon optional. Quotes are required around strings.
ROW_RE = re.compile(
    r"\(\s*"
    r"'((?:[^']|'')+)'"                 # phytochemical_name
    r"\s*,\s*ARRAY\[\s*([^\]]+)\s*\]"   # fruit_vegetables
    r"\s*,\s*ARRAY\[\s*([^\]]+)\s*\]"   # primary_sources
    r"\s*,\s*"
    r"'((?:[^']|'')+)'"                 # chemical_class
    r"\s*\)",
    re.MULTILINE,
)


def split_array(raw: str) -> list[str]:
    """Split an ARRAY[...] body like  'A','B','C'  into ['A','B','C']."""
    items = re.findall(r"'((?:[^']|'')*)'", raw)
    return [item.replace("''", "'").strip() for item in items if item.strip()]


def parse_sql(text: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for match in ROW_RE.finditer(text):
        name, fruits_raw, sources_raw, klass = match.groups()
        rows.append(
            {
                "phytochemical_name": name.replace("''", "'").strip(),
                "fruit_vegetables": split_array(fruits_raw),
                "primary_sources": split_array(sources_raw),
                "chemical_class": klass.replace("''", "'").strip(),
            }
        )
    return rows


def main() -> int:
    load_dotenv(ROOT / ".env")
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL and SUPABASE_KEY must be set in .env")
        return 1
    if not SQL_FILE.exists():
        print(f"ERROR: seed file not found: {SQL_FILE}")
        return 1

    text = SQL_FILE.read_text(encoding="utf-8")
    records = parse_sql(text)
    if not records:
        print("ERROR: parser extracted 0 rows — SQL format may have changed")
        return 1

    print(f"Parsed {len(records)} phytochemical rows from {SQL_FILE.name}")
    print(f"Target table: {TABLE}")
    print()

    client = create_client(url, key)
    start = time.time()
    inserted = 0

    for offset in range(0, len(records), BATCH_SIZE):
        batch = records[offset : offset + BATCH_SIZE]
        client.table(TABLE).upsert(batch, on_conflict="phytochemical_name").execute()
        inserted += len(batch)
        print(f"  upserted {inserted}/{len(records)}", flush=True)

    elapsed = time.time() - start
    print()
    print(f"Done in {elapsed:.1f}s ({inserted} rows)")

    try:
        resp = client.table(TABLE).select("id", count="exact").limit(1).execute()
        print(f"Table total: {resp.count}")
    except Exception as exc:
        print(f"(count check skipped: {exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
