"""Master orchestrator — runs all population scripts in sequence.

Executes each step as a subprocess so failures are isolated: a broken step
is logged and skipped rather than aborting the entire run.  Progress and
timings are written to data_population/logs/run_all_<date>.log and a final
summary is printed to stdout.

Usage:
    python data_population/run_all.py [--dry-run] [--skip-ctd] [--skip-usda]
                                      [--steps 1,2,3]   # run specific steps only
    python data_population/run_all.py --list            # show available steps
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from utils import LOG_DIR, REPORTS_DIR, setup_logging

logger = setup_logging("run_all")

DATA_DIR = Path(__file__).parent


# ── Step registry ─────────────────────────────────────────────────────────────

class Step(NamedTuple):
    number:      int
    name:        str
    script:      str        # relative to DATA_DIR
    description: str
    extra_flags: list[str]  # additional CLI flags forwarded from run_all


STEPS: list[Step] = [
    Step(
        1, "fetch_genes",
        "fetch_top_disease_genes.py",
        "Fetch top 200 disease-associated genes from Open Targets",
        [],
    ),
    Step(
        2, "kegg_pathways",
        "populate_kegg_pathways.py",
        "Populate pathways and gene_pathway_associations from KEGG",
        [],
    ),
    Step(
        3, "compounds",
        "populate_compound_data.py",
        "Populate 100+ natural food bioactives via PubChem validation",
        [],
    ),
    Step(
        4, "interactions",
        "populate_gene_compound_interactions.py",
        "Populate gene–compound interactions (curated seed + CTD enrichment)",
        ["--skip-ctd"],   # forwarded when --skip-ctd is passed to run_all
    ),
    Step(
        5, "foods",
        "populate_food_sources.py",
        "Populate 300+ foods and 5 000+ food–compound associations",
        ["--skip-usda"],  # forwarded when --skip-usda is passed to run_all
    ),
    Step(
        6, "validate",
        "validate_data_quality.py",
        "Run quality checks and generate HTML report",
        [],
    ),
]


# ── Step runner ───────────────────────────────────────────────────────────────

def run_step(
    step: Step,
    dry_run: bool,
    skip_ctd: bool,
    skip_usda: bool,
) -> tuple[bool, float]:
    """Execute a single step as a subprocess.

    Returns (success: bool, elapsed_seconds: float).
    """
    cmd = [sys.executable, str(DATA_DIR / step.script)]

    if dry_run:
        cmd.append("--dry-run")

    # Forward optional flags only to steps that accept them
    if skip_ctd and "--skip-ctd" in step.extra_flags:
        cmd.append("--skip-ctd")
    if skip_usda and "--skip-usda" in step.extra_flags:
        cmd.append("--skip-usda")

    logger.info(
        "── Step %d/%d: %s ──────────────────────────────",
        step.number, len(STEPS), step.name,
    )
    logger.info("Running: %s", " ".join(cmd))

    t0 = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(DATA_DIR.parent),   # project root so app.* imports resolve
            capture_output=False,        # let output stream to terminal in real time
            text=True,
            check=False,
        )
        elapsed = time.monotonic() - t0
        if result.returncode == 0:
            logger.info("Step %d completed successfully in %.1fs.", step.number, elapsed)
            return True, elapsed
        else:
            logger.error(
                "Step %d FAILED (exit code %d) after %.1fs.",
                step.number, result.returncode, elapsed,
            )
            return False, elapsed
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.error("Step %d raised an exception: %s", step.number, exc)
        return False, elapsed


# ── Summary report ────────────────────────────────────────────────────────────

def print_summary(
    results: list[tuple[Step, bool, float]],
    total_elapsed: float,
    dry_run: bool,
) -> None:
    sep = "═" * 60
    logger.info(sep)
    logger.info("POPULATION RUN SUMMARY%s", "  [DRY RUN]" if dry_run else "")
    logger.info(sep)

    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed

    for step, ok, elapsed in results:
        status = "✓ PASS" if ok else "✗ FAIL"
        logger.info(
            "  %s  Step %d %-20s  %.1fs",
            status, step.number, step.name, elapsed,
        )

    logger.info(sep)
    logger.info(
        "Total: %d/%d steps passed  |  wall time: %.1fs",
        passed, len(results), total_elapsed,
    )

    # Write plain-text summary to reports/
    summary_path = REPORTS_DIR / f"run_summary_{datetime.now():%Y%m%d_%H%M%S}.txt"
    lines = [
        f"NatureWellness Population Run — {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Mode: {'DRY RUN' if dry_run else 'LIVE'}",
        "",
    ]
    for step, ok, elapsed in results:
        lines.append(f"  {'PASS' if ok else 'FAIL'}  Step {step.number}: {step.description}  ({elapsed:.1f}s)")
    lines += [
        "",
        f"Passed: {passed}/{len(results)}",
        f"Wall time: {total_elapsed:.1f}s",
    ]
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Summary written to: %s", summary_path)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run all NatureWellness data population scripts in sequence.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",  action="store_true",
        help="Pass --dry-run to every script (preview without inserting)",
    )
    parser.add_argument(
        "--skip-ctd", action="store_true",
        help="Skip CTD API enrichment in populate_gene_compound_interactions.py",
    )
    parser.add_argument(
        "--skip-usda", action="store_true",
        help="Skip USDA API enrichment in populate_food_sources.py",
    )
    parser.add_argument(
        "--steps",
        help="Comma-separated step numbers to run, e.g. --steps 1,3,5  (default: all)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List available steps and exit",
    )
    args = parser.parse_args()

    if args.list:
        print("\nAvailable steps:")
        for s in STEPS:
            print(f"  {s.number}. {s.name:25s} — {s.description}")
        print()
        return 0

    # Filter steps if --steps was given
    selected_numbers: set[int] | None = None
    if args.steps:
        try:
            selected_numbers = {int(n.strip()) for n in args.steps.split(",")}
        except ValueError:
            logger.error("--steps must be comma-separated integers, e.g. --steps 1,3,5")
            return 1

    steps_to_run = [
        s for s in STEPS
        if selected_numbers is None or s.number in selected_numbers
    ]

    if not steps_to_run:
        logger.warning("No steps to run. Check --steps filter.")
        return 0

    logger.info(
        "Starting population run%s — %d step(s) selected.",
        " [DRY RUN]" if args.dry_run else "",
        len(steps_to_run),
    )

    wall_t0 = time.monotonic()
    results: list[tuple[Step, bool, float]] = []

    for step in steps_to_run:
        ok, elapsed = run_step(
            step,
            dry_run=args.dry_run,
            skip_ctd=args.skip_ctd,
            skip_usda=args.skip_usda,
        )
        results.append((step, ok, elapsed))
        # Continue on failure — don't abort the whole run

    total_elapsed = time.monotonic() - wall_t0
    print_summary(results, total_elapsed, dry_run=args.dry_run)

    failed_steps = [s for s, ok, _ in results if not ok]
    if failed_steps:
        logger.warning(
            "%d step(s) failed: %s",
            len(failed_steps),
            ", ".join(s.name for s in failed_steps),
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
