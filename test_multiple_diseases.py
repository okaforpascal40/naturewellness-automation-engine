"""Test the automation pipeline against 10 diseases concurrently.

Usage:
    python test_multiple_diseases.py [--sequential] [--base-url URL]
                                     [--max-genes N] [--min-score F]
                                     [--out-dir DIR]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

# ── Disease registry ──────────────────────────────────────────────────────────

DISEASES: list[tuple[str, str]] = [
    ("MONDO_0004975", "Alzheimer's Disease"),
    ("MONDO_0005180", "Parkinson's Disease"),
    ("MONDO_0005044", "Hypertension"),
    ("MONDO_0005010", "Coronary Artery Disease"),
    ("MONDO_0007254", "Breast Cancer"),
    ("MONDO_0008315", "Prostate Cancer"),
    ("MONDO_0008383", "Rheumatoid Arthritis"),
    ("MONDO_0005265", "Inflammatory Bowel Disease"),
    ("MONDO_0011122", "Obesity"),
    ("MONDO_0005300", "Chronic Kidney Disease"),
]


# ── Result container ──────────────────────────────────────────────────────────

@dataclass
class DiseaseResult:
    disease_id: str
    disease_name: str
    elapsed_s: float = 0.0
    status: str = "pending"        # ok | error | timeout
    error: str = ""
    genes_found: int = 0
    pathways_found: int = 0
    compounds_found: int = 0
    foods_found: int = 0
    evidence_scores: list[dict[str, Any]] = field(default_factory=list)
    http_status: int = 0

    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def top_foods(self) -> list[dict[str, Any]]:
        """Return top-5 evidence scores sorted by score descending."""
        return sorted(self.evidence_scores, key=lambda s: s.get("score", 0), reverse=True)[:5]

    @property
    def avg_score(self) -> float:
        if not self.evidence_scores:
            return 0.0
        return sum(s.get("score", 0) for s in self.evidence_scores) / len(self.evidence_scores)

    @property
    def high_evidence_count(self) -> int:
        return sum(1 for s in self.evidence_scores if s.get("evidence_level") == "high")

    @property
    def success(self) -> bool:
        return self.status == "ok"


# ── API caller ────────────────────────────────────────────────────────────────

async def call_disease(
    client: httpx.AsyncClient,
    disease_id: str,
    disease_name: str,
    base_url: str,
    max_genes: int,
    min_score: float,
) -> DiseaseResult:
    result = DiseaseResult(disease_id=disease_id, disease_name=disease_name)
    payload = {
        "disease_id":     disease_id,
        "disease_name":   disease_name,
        "max_genes":      max_genes,
        "min_gene_score": min_score,
    }
    t0 = time.perf_counter()
    try:
        resp = await client.post(f"{base_url}/api/v1/automation/run", json=payload)
        result.elapsed_s  = time.perf_counter() - t0
        result.http_status = resp.status_code

        if resp.status_code != 200:
            result.status = "error"
            try:
                detail = resp.json().get("detail", resp.text[:200])
            except Exception:
                detail = resp.text[:200]
            result.error = f"HTTP {resp.status_code}: {detail}"
            return result

        data = resp.json()
        result.status          = "ok"
        result.genes_found     = data.get("genes_found", 0)
        result.pathways_found  = data.get("pathways_found", 0)
        result.compounds_found = data.get("compounds_found", 0)
        result.foods_found     = data.get("foods_found", 0)
        result.evidence_scores = data.get("evidence_scores", [])
    except httpx.TimeoutException:
        result.elapsed_s = time.perf_counter() - t0
        result.status = "timeout"
        result.error  = "Request timed out"
    except httpx.ConnectError:
        result.elapsed_s = time.perf_counter() - t0
        result.status = "error"
        result.error  = "Connection refused — is the server running on localhost:8000?"
    except Exception as exc:
        result.elapsed_s = time.perf_counter() - t0
        result.status = "error"
        result.error  = str(exc)
    return result


# ── Console table ─────────────────────────────────────────────────────────────

_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_CYAN   = "\033[96m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"

def _status_color(r: DiseaseResult) -> str:
    if r.status == "ok":      return f"{_GREEN}OK{_RESET}"
    if r.status == "timeout": return f"{_YELLOW}TIMEOUT{_RESET}"
    return f"{_RED}ERROR{_RESET}"

def print_summary_table(results: list[DiseaseResult]) -> None:
    col_w = [30, 8, 6, 8, 9, 6, 8, 8]
    headers = ["Disease", "Status", "Genes", "Pathways", "Compounds", "Foods", "AvgScore", "Time(s)"]

    sep = "+" + "+".join("-" * (w + 2) for w in col_w) + "+"
    def row(*cells: str) -> str:
        parts = []
        for cell, w in zip(cells, col_w):
            # strip ANSI for width calculation
            visible = cell.replace(_GREEN,"").replace(_RED,"").replace(_YELLOW,"") \
                          .replace(_CYAN,"").replace(_BOLD,"").replace(_RESET,"")
            padding = w - len(visible)
            parts.append(f" {cell}{' ' * max(0, padding)} ")
        return "|" + "|".join(parts) + "|"

    print()
    print(f"{_BOLD}{'NatureWellness — Disease Test Results':^{sum(col_w) + len(col_w)*3 + 1}}{_RESET}")
    print(sep)
    print(row(*[f"{_BOLD}{h}{_RESET}" for h in headers]))
    print(sep)

    for r in results:
        name  = r.disease_name[:28]
        st    = _status_color(r)
        genes = str(r.genes_found) if r.success else "-"
        paths = str(r.pathways_found) if r.success else "-"
        cmpds = str(r.compounds_found) if r.success else "-"
        foods = str(r.foods_found) if r.success else "-"
        avg   = f"{r.avg_score:.1f}" if r.success else "-"
        t     = f"{r.elapsed_s:.1f}"
        print(row(name, st, genes, paths, cmpds, foods, avg, t))

    print(sep)

    # Footer stats
    n_ok      = sum(1 for r in results if r.success)
    n_total   = len(results)
    total_genes = sum(r.genes_found for r in results)
    total_foods = sum(r.foods_found for r in results)
    avg_time    = sum(r.elapsed_s   for r in results) / max(n_total, 1)

    print(f"\n{_BOLD}Summary:{_RESET}")
    print(f"  Success rate : {_GREEN}{n_ok}/{n_total}{_RESET} ({100*n_ok//n_total}%)")
    print(f"  Total genes  : {total_genes}")
    print(f"  Total foods  : {total_foods}")
    print(f"  Avg time/req : {avg_time:.1f}s")
    print()


def print_top_foods(results: list[DiseaseResult]) -> None:
    print(f"{_BOLD}Top food recommendations per disease:{_RESET}")
    for r in results:
        if not r.success or not r.evidence_scores:
            continue
        print(f"\n  {_CYAN}{r.disease_name}{_RESET}")
        for i, s in enumerate(r.top_foods, 1):
            food     = s.get("food_name", "?")
            score    = s.get("score", 0)
            level    = s.get("evidence_level", "?")
            compound = s.get("compound_name", "?")
            gene     = s.get("gene_symbol", "?")
            color = _GREEN if level == "high" else (_YELLOW if level == "moderate" else _RESET)
            print(f"    {i}. {food:<28} score={score:5.1f}  level={color}{level:<12}{_RESET}"
                  f"  via {compound} / {gene}")


# ── JSON export ───────────────────────────────────────────────────────────────

def save_json(results: list[DiseaseResult], path: Path) -> None:
    payload = {
        "generated_at": datetime.now().isoformat(),
        "total_diseases": len(results),
        "success_count": sum(1 for r in results if r.success),
        "results": [
            {
                "disease_id":      r.disease_id,
                "disease_name":    r.disease_name,
                "status":          r.status,
                "elapsed_s":       round(r.elapsed_s, 3),
                "http_status":     r.http_status,
                "error":           r.error,
                "genes_found":     r.genes_found,
                "pathways_found":  r.pathways_found,
                "compounds_found": r.compounds_found,
                "foods_found":     r.foods_found,
                "avg_score":       round(r.avg_score, 2),
                "high_evidence_count": r.high_evidence_count,
                "top_foods": r.top_foods,
                "all_evidence_scores": r.evidence_scores,
            }
            for r in results
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"JSON saved → {path}")


# ── HTML report ───────────────────────────────────────────────────────────────

def _badge(level: str) -> str:
    colors = {
        "high":         "#22c55e",
        "moderate":     "#f59e0b",
        "low":          "#94a3b8",
        "insufficient": "#ef4444",
    }
    c = colors.get(level, "#94a3b8")
    return (f'<span style="background:{c};color:#fff;padding:2px 8px;'
            f'border-radius:12px;font-size:11px;font-weight:600">{level}</span>')

def _status_badge(status: str) -> str:
    if status == "ok":
        return '<span style="background:#22c55e;color:#fff;padding:2px 8px;border-radius:12px;font-size:11px">OK</span>'
    if status == "timeout":
        return '<span style="background:#f59e0b;color:#fff;padding:2px 8px;border-radius:12px;font-size:11px">TIMEOUT</span>'
    return '<span style="background:#ef4444;color:#fff;padding:2px 8px;border-radius:12px;font-size:11px">ERROR</span>'


def save_html(results: list[DiseaseResult], path: Path, elapsed_total: float) -> None:
    n_ok    = sum(1 for r in results if r.success)
    n_total = len(results)
    pct     = int(100 * n_ok / max(n_total, 1))
    ts      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Summary rows ──────────────────────────────────────────────────────────
    summary_rows = ""
    for r in results:
        err_cell = (f'<td style="color:#ef4444;font-size:11px">{r.error[:80]}</td>'
                    if r.error else "<td>—</td>")
        summary_rows += f"""
        <tr>
          <td style="font-weight:600">{r.disease_name}</td>
          <td style="font-family:monospace;font-size:11px;color:#94a3b8">{r.disease_id}</td>
          <td>{_status_badge(r.status)}</td>
          <td style="text-align:center">{r.genes_found if r.success else "—"}</td>
          <td style="text-align:center">{r.pathways_found if r.success else "—"}</td>
          <td style="text-align:center">{r.compounds_found if r.success else "—"}</td>
          <td style="text-align:center">{r.foods_found if r.success else "—"}</td>
          <td style="text-align:center">{f"{r.avg_score:.1f}" if r.success and r.avg_score else "—"}</td>
          <td style="text-align:center">{r.elapsed_s:.1f}s</td>
          {err_cell}
        </tr>"""

    # ── Top foods section ─────────────────────────────────────────────────────
    food_sections = ""
    for r in results:
        if not r.success or not r.evidence_scores:
            continue
        food_rows = ""
        for i, s in enumerate(r.top_foods, 1):
            food_rows += f"""
            <tr>
              <td style="color:#94a3b8;text-align:center">{i}</td>
              <td style="font-weight:600">{s.get("food_name","?")}</td>
              <td>{s.get("compound_name","?")}</td>
              <td style="font-family:monospace">{s.get("gene_symbol","?")}</td>
              <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
                  title="{s.get('pathway_name','?')}">{s.get("pathway_name","?")}</td>
              <td style="text-align:center">
                <span style="font-weight:700;color:#38bdf8">{s.get("score",0):.1f}</span>
              </td>
              <td style="text-align:center">{_badge(s.get("evidence_level","?"))}</td>
            </tr>"""
        food_sections += f"""
        <div class="food-card">
          <h3>{r.disease_name}
            <small style="font-weight:400;color:#94a3b8;font-size:13px">
              — {len(r.evidence_scores)} evidence record(s)
            </small>
          </h3>
          <table>
            <thead>
              <tr>
                <th>#</th><th>Food</th><th>Compound</th>
                <th>Gene</th><th>Pathway</th><th>Score</th><th>Evidence</th>
              </tr>
            </thead>
            <tbody>{food_rows}</tbody>
          </table>
        </div>"""

    # ── Coverage bar ──────────────────────────────────────────────────────────
    # Compound coverage across diseases
    compound_counts: dict[str, int] = defaultdict(int)
    for r in results:
        for s in r.evidence_scores:
            c = s.get("compound_name", "")
            if c:
                compound_counts[c] += 1
    top_compounds = sorted(compound_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    compound_bars = ""
    max_count = top_compounds[0][1] if top_compounds else 1
    for cname, cnt in top_compounds:
        pct_bar = int(100 * cnt / max_count)
        compound_bars += f"""
        <div style="margin-bottom:6px">
          <span style="display:inline-block;width:200px;font-size:12px">{cname}</span>
          <span style="display:inline-block;width:{pct_bar * 2}px;height:14px;
                background:#38bdf8;border-radius:3px;vertical-align:middle"></span>
          <span style="margin-left:6px;color:#94a3b8;font-size:12px">{cnt} disease(s)</span>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>NatureWellness — Disease Test Results</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      margin: 0; padding: 24px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0f172a; color: #e2e8f0; font-size: 14px; line-height: 1.5;
    }}
    h1 {{ font-size: 22px; font-weight: 700; color: #f8fafc; margin: 0 0 4px; }}
    h2 {{ font-size: 16px; font-weight: 600; color: #cbd5e1; margin: 32px 0 12px; }}
    h3 {{ font-size: 15px; font-weight: 600; color: #f1f5f9; margin: 0 0 10px; }}
    small {{ font-size: 12px; }}
    .meta {{ color: #64748b; font-size: 12px; margin-bottom: 24px; }}
    .stat-grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 12px; margin-bottom: 28px;
    }}
    .stat-card {{
      background: #1e293b; border: 1px solid #334155;
      border-radius: 10px; padding: 14px 18px;
    }}
    .stat-card .val {{
      font-size: 28px; font-weight: 700; color: #38bdf8;
    }}
    .stat-card .lbl {{ color: #64748b; font-size: 12px; }}
    table {{
      width: 100%; border-collapse: collapse;
      background: #1e293b; border-radius: 10px; overflow: hidden;
    }}
    th {{
      background: #0f172a; color: #94a3b8; font-size: 11px;
      font-weight: 600; text-transform: uppercase; letter-spacing: .05em;
      padding: 10px 12px; text-align: left;
    }}
    td {{ padding: 9px 12px; border-top: 1px solid #1e293b; color: #cbd5e1; }}
    tr:hover td {{ background: #263348; }}
    .food-card {{
      background: #1e293b; border: 1px solid #334155;
      border-radius: 10px; padding: 16px; margin-bottom: 16px;
    }}
    .food-card table {{ background: transparent; }}
    .food-card td {{ border-top-color: #334155; }}
    .food-card th {{ background: #172033; }}
    .progress-ring {{
      display: inline-block; width: 60px; height: 60px;
      border-radius: 50%;
      background: conic-gradient(#22c55e {pct}%, #334155 0);
      position: relative;
    }}
    .progress-ring::after {{
      content: "{pct}%";
      position: absolute; inset: 8px;
      background: #1e293b; border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: 12px; font-weight: 700; color: #22c55e;
    }}
    .section {{ margin-bottom: 32px; }}
  </style>
</head>
<body>

<h1>🌿 NatureWellness Automation Engine</h1>
<p class="meta">Generated: {ts} &nbsp;|&nbsp; Total elapsed: {elapsed_total:.1f}s</p>

<!-- Stat cards -->
<div class="stat-grid">
  <div class="stat-card">
    <div class="val">{n_ok}/{n_total}</div>
    <div class="lbl">Diseases succeeded</div>
  </div>
  <div class="stat-card">
    <div class="val">{sum(r.genes_found for r in results)}</div>
    <div class="lbl">Total genes found</div>
  </div>
  <div class="stat-card">
    <div class="val">{sum(r.pathways_found for r in results)}</div>
    <div class="lbl">Total pathways</div>
  </div>
  <div class="stat-card">
    <div class="val">{sum(r.compounds_found for r in results)}</div>
    <div class="lbl">Total compounds</div>
  </div>
  <div class="stat-card">
    <div class="val">{sum(r.foods_found for r in results)}</div>
    <div class="lbl">Total foods</div>
  </div>
  <div class="stat-card">
    <div class="val">{sum(r.high_evidence_count for r in results)}</div>
    <div class="lbl">High-evidence records</div>
  </div>
  <div class="stat-card">
    <div class="val">{sum(r.elapsed_s for r in results) / max(n_total,1):.1f}s</div>
    <div class="lbl">Avg response time</div>
  </div>
  <div class="stat-card" style="display:flex;align-items:center;gap:12px">
    <div class="progress-ring"></div>
    <div class="lbl">Success rate</div>
  </div>
</div>

<!-- Summary table -->
<div class="section">
  <h2>Summary Table</h2>
  <table>
    <thead>
      <tr>
        <th>Disease</th><th>ID</th><th>Status</th>
        <th style="text-align:center">Genes</th>
        <th style="text-align:center">Pathways</th>
        <th style="text-align:center">Compounds</th>
        <th style="text-align:center">Foods</th>
        <th style="text-align:center">Avg Score</th>
        <th style="text-align:center">Time</th>
        <th>Error</th>
      </tr>
    </thead>
    <tbody>{summary_rows}</tbody>
  </table>
</div>

<!-- Top foods -->
<div class="section">
  <h2>Top Food Recommendations by Disease</h2>
  {food_sections if food_sections else '<p style="color:#64748b">No successful results to display.</p>'}
</div>

<!-- Compound coverage -->
<div class="section">
  <h2>Top Compounds by Disease Coverage</h2>
  <div style="background:#1e293b;border:1px solid #334155;border-radius:10px;padding:16px">
    {compound_bars if compound_bars else '<p style="color:#64748b">No data.</p>'}
  </div>
</div>

</body>
</html>"""

    path.write_text(html, encoding="utf-8")
    print(f"HTML saved → {path}")


# ── Runner ────────────────────────────────────────────────────────────────────

async def run_all(
    base_url: str,
    max_genes: int,
    min_score: float,
    sequential: bool,
    timeout: float,
) -> list[DiseaseResult]:
    limits  = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    t_cfg   = httpx.Timeout(timeout, connect=10.0)

    async with httpx.AsyncClient(limits=limits, timeout=t_cfg) as client:
        if sequential:
            results: list[DiseaseResult] = []
            for disease_id, disease_name in DISEASES:
                print(f"  Testing {disease_name} …", end=" ", flush=True)
                r = await call_disease(client, disease_id, disease_name,
                                       base_url, max_genes, min_score)
                status_str = "OK" if r.success else r.status.upper()
                print(f"{status_str} ({r.elapsed_s:.1f}s)")
                results.append(r)
        else:
            tasks = [
                call_disease(client, disease_id, disease_name,
                             base_url, max_genes, min_score)
                for disease_id, disease_name in DISEASES
            ]
            print(f"  Running {len(tasks)} requests concurrently …")
            results = await asyncio.gather(*tasks)

    return list(results)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="Test automation API against 10 diseases.")
    parser.add_argument("--sequential",  action="store_true",
                        help="Test one disease at a time instead of concurrently")
    parser.add_argument("--base-url",    default="http://localhost:8000",
                        help="Base URL of the FastAPI server (default: http://localhost:8000)")
    parser.add_argument("--max-genes",   type=int,   default=10,
                        help="max_genes per pipeline run (default: 10)")
    parser.add_argument("--min-score",   type=float, default=0.3,
                        help="min_gene_score filter (default: 0.3)")
    parser.add_argument("--timeout",     type=float, default=120.0,
                        help="Per-request timeout in seconds (default: 120)")
    parser.add_argument("--out-dir",     default=".",
                        help="Directory to write JSON and HTML reports (default: .)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ts_str   = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"test_results_{ts_str}.json"
    html_path = out_dir / f"test_results_{ts_str}.html"

    mode = "sequential" if args.sequential else "concurrent"
    print(f"\n{'=' * 60}")
    print(f"  NatureWellness Disease Test Runner")
    print(f"  Mode    : {mode}")
    print(f"  Server  : {args.base_url}")
    print(f"  Diseases: {len(DISEASES)}")
    print(f"  Params  : max_genes={args.max_genes}, min_score={args.min_score}")
    print(f"{'=' * 60}\n")

    # Health check
    try:
        async with httpx.AsyncClient(timeout=5.0) as hc:
            hr = await hc.get(f"{args.base_url}/health")
            if hr.status_code == 200:
                print(f"  Health check: OK\n")
            else:
                print(f"  Health check: HTTP {hr.status_code} — continuing anyway\n")
    except Exception as exc:
        print(f"  Health check FAILED: {exc}")
        print(f"  Make sure the server is running: uvicorn app.main:app --reload\n")

    t_wall = time.perf_counter()
    results = await run_all(
        base_url   = args.base_url,
        max_genes  = args.max_genes,
        min_score  = args.min_score,
        sequential = args.sequential,
        timeout    = args.timeout,
    )
    elapsed_total = time.perf_counter() - t_wall

    # Console output
    print_summary_table(results)
    print_top_foods(results)

    # Persist reports
    print()
    save_json(results, json_path)
    save_html(results, html_path, elapsed_total)

    # Exit code: non-zero if any disease failed
    failed = [r for r in results if not r.success]
    if failed:
        print(f"\n  {len(failed)} disease(s) failed: "
              f"{', '.join(r.disease_name for r in failed)}")
        sys.exit(1)
    else:
        print(f"\n  All {len(results)} diseases completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
