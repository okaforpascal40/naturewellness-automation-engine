"""Stream-download CTD's chemical-gene interactions bulk file and filter to
phytochemicals + human (OrganismID 9606) in one pass.

Output: scripts/ctd_phytochemical_interactions_filtered.tsv

Usage:
    python scripts/filter_ctd_bulk.py
"""
from __future__ import annotations

import gzip
import io
import sys
import time
import urllib.request
from pathlib import Path

# Make the app importable when running this script directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.api.ctd_api import _PHYTOCHEMICAL_NAMES, _PHYTOCHEMICAL_PATTERNS  # noqa: E402

CTD_URL = "https://ctdbase.org/reports/CTD_chem_gene_ixns.tsv.gz"
OUTPUT_PATH = ROOT / "scripts" / "ctd_phytochemical_interactions_filtered.tsv"

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

HUMAN_TAXON = "9606"


def is_phytochemical(name: str) -> bool:
    lname = name.lower()
    if any(p in lname for p in _PHYTOCHEMICAL_NAMES):
        return True
    return bool(_PHYTOCHEMICAL_PATTERNS.search(lname))


def main() -> int:
    print(f"Downloading: {CTD_URL}")
    print(f"Whitelist size: {len(_PHYTOCHEMICAL_NAMES)} curated names + class regex")
    print()

    start = time.time()
    rows_in = 0
    rows_kept = 0
    bytes_in = 0
    last_log = start

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    req = urllib.request.Request(
        CTD_URL,
        headers={"User-Agent": "naturewellness-automation-engine/1.0 (+filter)"},
    )

    with urllib.request.urlopen(req, timeout=600) as resp:
        size_hdr = resp.headers.get("Content-Length")
        if size_hdr:
            print(f"Compressed size: {int(size_hdr) / (1024 * 1024):.1f} MB")

        # gzip.open over the response stream → text-mode decode
        with gzip.GzipFile(fileobj=resp) as gz, OUTPUT_PATH.open(
            "w", encoding="utf-8", newline=""
        ) as out:
            text_stream = io.TextIOWrapper(gz, encoding="utf-8", errors="replace")

            # Header
            out.write("\t".join(CTD_COLUMNS) + "\n")

            for line in text_stream:
                bytes_in += len(line)
                if not line or line.startswith("#"):
                    continue

                # CTD TSV is tab-separated with exactly the columns above.
                fields = line.rstrip("\n").split("\t")
                if len(fields) < len(CTD_COLUMNS):
                    continue
                rows_in += 1

                chem_name = fields[0].strip()
                organism_id = fields[7].strip()

                if organism_id != HUMAN_TAXON:
                    continue
                if not chem_name or not is_phytochemical(chem_name):
                    continue

                out.write("\t".join(fields[: len(CTD_COLUMNS)]) + "\n")
                rows_kept += 1

                now = time.time()
                if now - last_log >= 5.0:
                    elapsed = now - start
                    print(
                        f"  ... read {rows_in:>9,} rows | kept {rows_kept:>7,}"
                        f" | {bytes_in / (1024 * 1024):>6.1f} MB decompressed"
                        f" | {elapsed:>5.1f}s",
                        flush=True,
                    )
                    last_log = now

    elapsed = time.time() - start
    print()
    print(f"Done in {elapsed:.1f}s")
    print(f"  Rows read   : {rows_in:,}")
    print(f"  Rows kept   : {rows_kept:,}")
    print(f"  Output      : {OUTPUT_PATH}")
    print(f"  Output size : {OUTPUT_PATH.stat().st_size / (1024 * 1024):.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
