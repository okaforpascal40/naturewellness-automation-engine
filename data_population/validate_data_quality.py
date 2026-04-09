"""Step 6 — Validate data quality and generate an HTML report.

Runs a suite of checks against the Supabase database:
  - Record counts per table vs. targets
  - Orphaned junction rows (FK with no matching parent)
  - Value-range violations (scores outside [0,1], concentrations ≤ 0)
  - Duplicate detection
  - Junction-table coverage (% of genes/compounds with linked data)

Writes a self-contained HTML report to data_population/reports/.

Usage:
    python data_population/validate_data_quality.py [--out reports/quality.html]
"""
from __future__ import annotations

import argparse
import asyncio
import html
from datetime import datetime
from pathlib import Path
from typing import Any

from utils import REPORTS_DIR, setup_logging

logger = setup_logging("validate")

# ── Targets ───────────────────────────────────────────────────────────────────

TARGETS: dict[str, int] = {
    "genes":                      200,
    "pathways":                   100,
    "compounds":                  100,
    "foods":                      300,
    "gene_pathway_associations":  2000,
    "compound_gene_interactions": 3000,
    "food_compound_associations": 5000,
}


# ── Check runners ─────────────────────────────────────────────────────────────

async def check_counts(client: Any) -> list[dict[str, Any]]:
    """Return count vs. target for every table."""
    results = []
    for table, target in TARGETS.items():
        resp = await client.table(table).select("id", count="exact").execute()
        count = resp.count or 0
        pct   = round(count / target * 100, 1) if target else 0
        results.append({
            "table":  table,
            "count":  count,
            "target": target,
            "pct":    pct,
            "ok":     count >= target,
        })
    return results


async def check_orphans(client: Any) -> list[dict[str, Any]]:
    """Detect junction rows whose FK columns reference non-existent parents."""
    issues: list[dict[str, Any]] = []

    # gene_pathway_associations → genes
    resp = await client.table("gene_pathway_associations").select("gene_id").execute()
    assoc_gene_ids = {r["gene_id"] for r in (resp.data or [])}
    resp = await client.table("genes").select("id").execute()
    valid_gene_ids = {r["id"] for r in (resp.data or [])}
    orphan_count = len(assoc_gene_ids - valid_gene_ids)
    issues.append({
        "check":  "gene_pathway_associations.gene_id → genes.id",
        "orphans": orphan_count,
        "ok":      orphan_count == 0,
    })

    # gene_pathway_associations → pathways
    resp = await client.table("gene_pathway_associations").select("pathway_id").execute()
    assoc_pw_ids = {r["pathway_id"] for r in (resp.data or [])}
    resp = await client.table("pathways").select("id").execute()
    valid_pw_ids = {r["id"] for r in (resp.data or [])}
    orphan_count = len(assoc_pw_ids - valid_pw_ids)
    issues.append({
        "check":  "gene_pathway_associations.pathway_id → pathways.id",
        "orphans": orphan_count,
        "ok":      orphan_count == 0,
    })

    # compound_gene_interactions → genes
    resp = await client.table("compound_gene_interactions").select("gene_id").execute()
    cgi_gene_ids = {r["gene_id"] for r in (resp.data or [])}
    orphan_count = len(cgi_gene_ids - valid_gene_ids)
    issues.append({
        "check":  "compound_gene_interactions.gene_id → genes.id",
        "orphans": orphan_count,
        "ok":      orphan_count == 0,
    })

    # compound_gene_interactions → compounds
    resp = await client.table("compound_gene_interactions").select("compound_id").execute()
    cgi_cmpd_ids = {r["compound_id"] for r in (resp.data or [])}
    resp = await client.table("compounds").select("id").execute()
    valid_cmpd_ids = {r["id"] for r in (resp.data or [])}
    orphan_count = len(cgi_cmpd_ids - valid_cmpd_ids)
    issues.append({
        "check":  "compound_gene_interactions.compound_id → compounds.id",
        "orphans": orphan_count,
        "ok":      orphan_count == 0,
    })

    # food_compound_associations → foods
    resp = await client.table("food_compound_associations").select("food_id").execute()
    fca_food_ids = {r["food_id"] for r in (resp.data or [])}
    resp = await client.table("foods").select("id").execute()
    valid_food_ids = {r["id"] for r in (resp.data or [])}
    orphan_count = len(fca_food_ids - valid_food_ids)
    issues.append({
        "check":  "food_compound_associations.food_id → foods.id",
        "orphans": orphan_count,
        "ok":      orphan_count == 0,
    })

    # food_compound_associations → compounds
    resp = await client.table("food_compound_associations").select("compound_id").execute()
    fca_cmpd_ids = {r["compound_id"] for r in (resp.data or [])}
    orphan_count = len(fca_cmpd_ids - valid_cmpd_ids)
    issues.append({
        "check":  "food_compound_associations.compound_id → compounds.id",
        "orphans": orphan_count,
        "ok":      orphan_count == 0,
    })

    return issues


async def check_ranges(client: Any) -> list[dict[str, Any]]:
    """Check numeric columns for out-of-range values."""
    issues: list[dict[str, Any]] = []

    # compound_gene_interactions.bioactivity_score should be in [0, 1]
    resp = await client.table("compound_gene_interactions") \
        .select("id, bioactivity_score") \
        .execute()
    bad = [
        r for r in (resp.data or [])
        if r.get("bioactivity_score") is not None
        and not (0.0 <= float(r["bioactivity_score"]) <= 1.0)
    ]
    issues.append({
        "check": "compound_gene_interactions.bioactivity_score ∈ [0, 1]",
        "violations": len(bad),
        "ok": len(bad) == 0,
    })

    # gene_pathway_associations.confidence_score should be in [0, 1]
    resp = await client.table("gene_pathway_associations") \
        .select("id, confidence_score") \
        .execute()
    bad = [
        r for r in (resp.data or [])
        if r.get("confidence_score") is not None
        and not (0.0 <= float(r["confidence_score"]) <= 1.0)
    ]
    issues.append({
        "check": "gene_pathway_associations.confidence_score ∈ [0, 1]",
        "violations": len(bad),
        "ok": len(bad) == 0,
    })

    # food_compound_associations.concentration_mg_per_100g should be > 0
    resp = await client.table("food_compound_associations") \
        .select("id, concentration_mg_per_100g") \
        .execute()
    bad = [
        r for r in (resp.data or [])
        if r.get("concentration_mg_per_100g") is not None
        and float(r["concentration_mg_per_100g"]) <= 0
    ]
    issues.append({
        "check": "food_compound_associations.concentration_mg_per_100g > 0",
        "violations": len(bad),
        "ok": len(bad) == 0,
    })

    return issues


async def check_duplicates(client: Any) -> list[dict[str, Any]]:
    """Surface obvious duplicate candidates using Supabase RPC if available,
    falling back to client-side detection."""
    issues: list[dict[str, Any]] = []

    # Duplicate gene_symbols in genes
    resp = await client.table("genes").select("gene_symbol").execute()
    symbols = [r["gene_symbol"] for r in (resp.data or []) if r.get("gene_symbol")]
    dup_count = len(symbols) - len(set(symbols))
    issues.append({
        "check": "genes.gene_symbol — duplicate symbols",
        "duplicates": dup_count,
        "ok": dup_count == 0,
    })

    # Duplicate compound_names in compounds
    resp = await client.table("compounds").select("compound_name").execute()
    names = [r["compound_name"] for r in (resp.data or []) if r.get("compound_name")]
    dup_count = len(names) - len(set(names))
    issues.append({
        "check": "compounds.compound_name — duplicate names",
        "duplicates": dup_count,
        "ok": dup_count == 0,
    })

    # Duplicate food names in foods
    resp = await client.table("foods").select("name").execute()
    names = [r["name"] for r in (resp.data or []) if r.get("name")]
    dup_count = len(names) - len(set(names))
    issues.append({
        "check": "foods.name — duplicate names",
        "duplicates": dup_count,
        "ok": dup_count == 0,
    })

    # Duplicate (gene_id, pathway_id) pairs
    resp = await client.table("gene_pathway_associations") \
        .select("gene_id, pathway_id").execute()
    pairs = [(r["gene_id"], r["pathway_id"]) for r in (resp.data or [])]
    dup_count = len(pairs) - len(set(pairs))
    issues.append({
        "check": "gene_pathway_associations — duplicate (gene_id, pathway_id) pairs",
        "duplicates": dup_count,
        "ok": dup_count == 0,
    })

    # Duplicate (compound_id, gene_id) pairs
    resp = await client.table("compound_gene_interactions") \
        .select("compound_id, gene_id").execute()
    pairs = [(r["compound_id"], r["gene_id"]) for r in (resp.data or [])]
    dup_count = len(pairs) - len(set(pairs))
    issues.append({
        "check": "compound_gene_interactions — duplicate (compound_id, gene_id) pairs",
        "duplicates": dup_count,
        "ok": dup_count == 0,
    })

    return issues


async def check_coverage(client: Any) -> list[dict[str, Any]]:
    """Report what % of genes/compounds have at least one linked row."""
    results: list[dict[str, Any]] = []

    # Genes with ≥1 pathway
    g_resp = await client.table("genes").select("id", count="exact").execute()
    total_genes = g_resp.count or 0
    p_resp = await client.table("gene_pathway_associations").select("gene_id").execute()
    genes_with_pathways = len({r["gene_id"] for r in (p_resp.data or [])})
    pct = round(genes_with_pathways / total_genes * 100, 1) if total_genes else 0
    results.append({
        "metric": "Genes with ≥1 pathway association",
        "value": f"{genes_with_pathways} / {total_genes}",
        "pct": pct,
        "ok": pct >= 50,
    })

    # Genes with ≥1 compound interaction
    c_resp = await client.table("compound_gene_interactions").select("gene_id").execute()
    genes_with_compounds = len({r["gene_id"] for r in (c_resp.data or [])})
    pct = round(genes_with_compounds / total_genes * 100, 1) if total_genes else 0
    results.append({
        "metric": "Genes with ≥1 compound interaction",
        "value": f"{genes_with_compounds} / {total_genes}",
        "pct": pct,
        "ok": pct >= 30,
    })

    # Compounds with ≥1 food association
    cmpd_r = await client.table("compounds").select("id", count="exact").execute()
    total_cmpds = cmpd_r.count or 0
    f_resp = await client.table("food_compound_associations").select("compound_id").execute()
    cmpds_with_foods = len({r["compound_id"] for r in (f_resp.data or [])})
    pct = round(cmpds_with_foods / total_cmpds * 100, 1) if total_cmpds else 0
    results.append({
        "metric": "Compounds with ≥1 food association",
        "value": f"{cmpds_with_foods} / {total_cmpds}",
        "pct": pct,
        "ok": pct >= 50,
    })

    # Foods with ≥1 compound
    food_r = await client.table("foods").select("id", count="exact").execute()
    total_foods = food_r.count or 0
    fc_resp = await client.table("food_compound_associations").select("food_id").execute()
    foods_with_cmpds = len({r["food_id"] for r in (fc_resp.data or [])})
    pct = round(foods_with_cmpds / total_foods * 100, 1) if total_foods else 0
    results.append({
        "metric": "Foods with ≥1 compound association",
        "value": f"{foods_with_cmpds} / {total_foods}",
        "pct": pct,
        "ok": pct >= 40,
    })

    return results


# ── HTML report ───────────────────────────────────────────────────────────────

def _badge(ok: bool) -> str:
    if ok:
        return '<span class="badge pass">PASS</span>'
    return '<span class="badge fail">FAIL</span>'


def _pct_bar(pct: float) -> str:
    color = "#22c55e" if pct >= 80 else "#f59e0b" if pct >= 50 else "#ef4444"
    return (
        f'<div class="bar-wrap"><div class="bar" '
        f'style="width:{min(pct,100):.0f}%;background:{color}"></div>'
        f'<span class="bar-label">{pct:.1f}%</span></div>'
    )


def build_html_report(
    counts:     list[dict[str, Any]],
    orphans:    list[dict[str, Any]],
    ranges:     list[dict[str, Any]],
    duplicates: list[dict[str, Any]],
    coverage:   list[dict[str, Any]],
) -> str:
    total_checks = len(counts) + len(orphans) + len(ranges) + len(duplicates) + len(coverage)
    passed       = sum(1 for c in counts + orphans + ranges + duplicates + coverage if c.get("ok"))
    overall_ok   = passed == total_checks
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Count section
    count_rows = "\n".join(
        f"<tr>"
        f"<td>{html.escape(r['table'])}</td>"
        f"<td class='num'>{r['count']:,}</td>"
        f"<td class='num'>{r['target']:,}</td>"
        f"<td>{_pct_bar(r['pct'])}</td>"
        f"<td>{_badge(r['ok'])}</td>"
        f"</tr>"
        for r in counts
    )

    # Orphan section
    orphan_rows = "\n".join(
        f"<tr>"
        f"<td>{html.escape(r['check'])}</td>"
        f"<td class='num'>{r['orphans']:,}</td>"
        f"<td>{_badge(r['ok'])}</td>"
        f"</tr>"
        for r in orphans
    )

    # Range section
    range_rows = "\n".join(
        f"<tr>"
        f"<td>{html.escape(r['check'])}</td>"
        f"<td class='num'>{r['violations']:,}</td>"
        f"<td>{_badge(r['ok'])}</td>"
        f"</tr>"
        for r in ranges
    )

    # Duplicate section
    dup_rows = "\n".join(
        f"<tr>"
        f"<td>{html.escape(r['check'])}</td>"
        f"<td class='num'>{r['duplicates']:,}</td>"
        f"<td>{_badge(r['ok'])}</td>"
        f"</tr>"
        for r in duplicates
    )

    # Coverage section
    cov_rows = "\n".join(
        f"<tr>"
        f"<td>{html.escape(r['metric'])}</td>"
        f"<td class='num'>{r['value']}</td>"
        f"<td>{_pct_bar(r['pct'])}</td>"
        f"<td>{_badge(r['ok'])}</td>"
        f"</tr>"
        for r in coverage
    )

    status_class = "overall-pass" if overall_ok else "overall-fail"
    status_text  = "ALL CHECKS PASSED" if overall_ok else f"{passed}/{total_checks} CHECKS PASSED"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NatureWellness — Data Quality Report</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #0f172a; color: #e2e8f0; padding: 2rem; }}
  h1   {{ font-size: 1.6rem; font-weight: 700; margin-bottom: .25rem; color: #f8fafc; }}
  .sub {{ color: #94a3b8; font-size: .875rem; margin-bottom: 2rem; }}
  .overall {{ padding: 1rem 1.5rem; border-radius: 8px; font-weight: 700;
              font-size: 1.1rem; margin-bottom: 2rem; display: inline-block; }}
  .overall-pass {{ background: #14532d; color: #4ade80; border: 1px solid #166534; }}
  .overall-fail {{ background: #450a0a; color: #f87171; border: 1px solid #7f1d1d; }}
  section {{ margin-bottom: 2rem; }}
  h2 {{ font-size: 1rem; font-weight: 600; color: #cbd5e1; margin-bottom: .75rem;
         text-transform: uppercase; letter-spacing: .05em; }}
  table  {{ width: 100%; border-collapse: collapse; background: #1e293b;
             border-radius: 8px; overflow: hidden; font-size: .875rem; }}
  th     {{ background: #0f172a; color: #64748b; font-weight: 600; padding: .6rem 1rem;
             text-align: left; font-size: .75rem; text-transform: uppercase; }}
  td     {{ padding: .6rem 1rem; border-bottom: 1px solid #334155; color: #cbd5e1; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #273548; }}
  .num   {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .badge {{ padding: .2rem .6rem; border-radius: 4px; font-size: .75rem;
             font-weight: 700; }}
  .pass  {{ background: #14532d; color: #4ade80; }}
  .fail  {{ background: #450a0a; color: #f87171; }}
  .bar-wrap {{ display: flex; align-items: center; gap: .5rem; }}
  .bar  {{ height: 8px; border-radius: 4px; min-width: 4px; transition: width .3s; }}
  .bar-label {{ font-size: .75rem; color: #94a3b8; white-space: nowrap; }}
  footer {{ margin-top: 3rem; font-size: .75rem; color: #475569; text-align: center; }}
</style>
</head>
<body>
<h1>NatureWellness — Data Quality Report</h1>
<p class="sub">Generated {generated_at}</p>
<div class="overall {status_class}">{status_text}</div>

<section>
  <h2>1. Record Counts vs. Targets</h2>
  <table>
    <thead><tr><th>Table</th><th>Count</th><th>Target</th><th>Progress</th><th>Status</th></tr></thead>
    <tbody>{count_rows}</tbody>
  </table>
</section>

<section>
  <h2>2. Orphaned Foreign Keys</h2>
  <table>
    <thead><tr><th>Relationship</th><th>Orphans</th><th>Status</th></tr></thead>
    <tbody>{orphan_rows}</tbody>
  </table>
</section>

<section>
  <h2>3. Value Range Violations</h2>
  <table>
    <thead><tr><th>Check</th><th>Violations</th><th>Status</th></tr></thead>
    <tbody>{range_rows}</tbody>
  </table>
</section>

<section>
  <h2>4. Duplicate Detection</h2>
  <table>
    <thead><tr><th>Check</th><th>Duplicates</th><th>Status</th></tr></thead>
    <tbody>{dup_rows}</tbody>
  </table>
</section>

<section>
  <h2>5. Junction Table Coverage</h2>
  <table>
    <thead><tr><th>Metric</th><th>Count</th><th>Coverage</th><th>Status</th></tr></thead>
    <tbody>{cov_rows}</tbody>
  </table>
</section>

<footer>NatureWellness Automation Engine — Conference Readiness Report</footer>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(out_path: Path) -> None:
    from app.database import get_supabase

    logger.info("Running data quality checks …")
    client = await get_supabase()

    counts     = await check_counts(client)
    orphans    = await check_orphans(client)
    ranges     = await check_ranges(client)
    duplicates = await check_duplicates(client)
    coverage   = await check_coverage(client)

    # Console summary
    total   = len(counts) + len(orphans) + len(ranges) + len(duplicates) + len(coverage)
    passed  = sum(1 for c in counts + orphans + ranges + duplicates + coverage if c.get("ok"))
    failed  = total - passed

    logger.info("Results: %d/%d checks passed, %d failed.", passed, total, failed)

    for section, items in [
        ("Counts",     counts),
        ("Orphans",    orphans),
        ("Ranges",     ranges),
        ("Duplicates", duplicates),
        ("Coverage",   coverage),
    ]:
        for item in items:
            if not item.get("ok"):
                label = item.get("check") or item.get("table") or item.get("metric", "")
                logger.warning("[FAIL] %s: %s", section, label)

    # Write HTML report
    report_html = build_html_report(counts, orphans, ranges, duplicates, coverage)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report_html, encoding="utf-8")
    logger.info("HTML report written to: %s", out_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate database quality and generate report.")
    parser.add_argument(
        "--out",
        type=Path,
        default=REPORTS_DIR / f"quality_{datetime.now():%Y%m%d_%H%M%S}.html",
        help="Output path for the HTML report",
    )
    args = parser.parse_args()
    asyncio.run(main(out_path=args.out))
