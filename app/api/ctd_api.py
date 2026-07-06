"""CTD (Comparative Toxicogenomics Database) — phytochemical-gene interactions.

Data source: pre-loaded snapshot of CTD's bulk file
    https://ctdbase.org/reports/CTD_chem_gene_ixns.tsv.gz
filtered to phytochemicals (whitelist below) and human (taxon 9606), seeded
into the Supabase table `ctd_phytochemical_gene_interactions`.

The live batchQuery endpoint is gated by an Altcha CAPTCHA on programmatic
access, so this module reads only from Supabase. To refresh the snapshot:

    python scripts/filter_ctd_bulk.py
    python scripts/seed_ctd_interactions.py

The whitelist constants (_PHYTOCHEMICAL_NAMES, _PHYTOCHEMICAL_PATTERNS) are
kept here because the filter script imports them.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Supabase table seeded by scripts/seed_ctd_interactions.py
_TABLE = "ctd_phytochemical_gene_interactions"

# Cap to avoid sending an oversized .in_() filter to PostgREST.
_MAX_GENES_PER_REQUEST = 100
_MAX_CHEMICALS_PER_REQUEST = 100

# --- Phytochemical whitelist ------------------------------------------------
# Curated dietary bioactives commonly studied in food/nutrition research.
# Names are matched case-insensitively as either exact strings or substrings
# of the CTD ChemicalName.
_PHYTOCHEMICAL_NAMES: frozenset[str] = frozenset(
    name.lower() for name in (
        # Flavonoids
        "Quercetin", "Kaempferol", "Myricetin", "Apigenin", "Luteolin",
        "Naringenin", "Hesperidin", "Hesperetin", "Rutin", "Fisetin",
        "Genistein", "Daidzein", "Glycitein", "Catechin", "Epicatechin",
        "Epigallocatechin", "Epigallocatechin gallate", "Cyanidin",
        "Delphinidin", "Malvidin", "Pelargonidin", "Peonidin", "Petunidin",
        # Polyphenols / stilbenes
        "Resveratrol", "Pterostilbene", "Curcumin", "Ellagic acid",
        "Gallic acid", "Caffeic acid", "Chlorogenic acid", "Ferulic acid",
        "Rosmarinic acid", "Tannic acid",
        # Carotenoids
        "Beta-carotene", "Alpha-carotene", "Lycopene", "Lutein", "Zeaxanthin",
        "Astaxanthin", "Cryptoxanthin", "Fucoxanthin",
        # Terpenes / terpenoids
        "Limonene", "Linalool", "Carvacrol", "Thymol", "Geraniol", "Menthol",
        "Carnosic acid", "Carnosol", "Ursolic acid", "Oleanolic acid",
        "Betulinic acid",
        # Organosulfur
        "Allicin", "Diallyl sulfide", "Diallyl disulfide", "Sulforaphane",
        "Indole-3-carbinol", "Diindolylmethane",
        # Alkaloids / xanthines
        "Caffeine", "Theobromine", "Theophylline", "Capsaicin", "Piperine",
        "Berberine",
        # Other notable phytochemicals
        "Lignan", "Enterolactone", "Enterodiol", "Secoisolariciresinol",
        "Silymarin", "Silibinin", "Hydroxytyrosol", "Oleuropein",
        "Phloretin", "Phlorizin", "Xanthohumol", "Honokiol", "Magnolol",
        "Withaferin A", "Gingerol", "Shogaol", "Zingerone", "Eugenol",
        "Vanillin", "Anethole",
    )
)

# Regex catching common phytochemical class suffixes / patterns. Used as a
# fallback after the whitelist check.
_PHYTOCHEMICAL_PATTERNS = re.compile(
    r"\b("
    r"flavon\w*|flavan\w*|flavanon\w*|isoflavon\w*|"
    r"anthocyan\w*|catechin\w*|polyphenol\w*|"
    r"phytoestrogen\w*|phytosterol\w*|carotenoid\w*|"
    r"glucosinolat\w*|glucoside|saponin\w*|"
    r"terpene\w*|terpenoid\w*|stilbene\w*|tannin\w*|lignan\w*"
    r")\b",
    re.IGNORECASE,
)


async def get_chemicals_for_genes(
    gene_symbols: list[str],
) -> list[dict[str, Any]]:
    """Look up phytochemical-gene interactions from the Supabase snapshot.

    Returns one record per (chemical, gene, interaction) row in the table,
    matching the shape the live API used to return:
        chemical_name, gene_symbol, interaction_type, publication_count, pmids
    """
    if not gene_symbols:
        return []

    # Deduplicate, uppercase (CTD GeneSymbol values are HGNC-style uppercase).
    seen: set[str] = set()
    deduped: list[str] = []
    for sym in gene_symbols:
        s = (sym or "").strip().upper()
        if s and s not in seen:
            seen.add(s)
            deduped.append(s)

    # Local import to avoid circular import at module load time.
    from app.database import get_supabase

    try:
        client = await get_supabase()
    except Exception as exc:
        logger.error("CTD lookup: Supabase client unavailable: %s", exc)
        return []

    rows: list[dict[str, Any]] = []
    for batch_start in range(0, len(deduped), _MAX_GENES_PER_REQUEST):
        batch = deduped[batch_start : batch_start + _MAX_GENES_PER_REQUEST]
        try:
            resp = (
                await client.table(_TABLE)
                .select("chemical_name,gene_symbol,interaction_actions,pubmed_ids,publication_count")
                .in_("gene_symbol", batch)
                .execute()
            )
        except Exception as exc:
            logger.error("CTD Supabase query failed: %s", exc)
            continue
        rows.extend(resp.data or [])

    results = [_normalize_row(r) for r in rows if r]
    logger.info(
        "CTD: %d phytochemical interaction(s) found across %d gene(s)",
        len(results),
        len(deduped),
    )
    return results


async def get_genes_for_chemicals(
    chemical_names: list[str],
) -> list[dict[str, Any]]:
    """Reverse lookup — gene interactions for a set of phytochemicals.

    Symmetric to :func:`get_chemicals_for_genes`; used by the CamScan reverse
    pipeline (plant → phytochemicals → genes). Matching is case-insensitive:
    CTD ChemicalName casing may differ from the phytochemical_sources table
    (e.g. "Curcumin" vs "curcumin"), so misses from the exact ``.in_()`` filter
    are retried per-name with ``.ilike()``.

    Returns one record per (chemical, gene, interaction) row, in the same shape
    as :func:`get_chemicals_for_genes`.
    """
    if not chemical_names:
        return []

    seen: set[str] = set()
    deduped: list[str] = []
    for name in chemical_names:
        s = (name or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            deduped.append(s)

    # Local import to avoid circular import at module load time.
    from app.database import get_supabase

    try:
        client = await get_supabase()
    except Exception as exc:
        logger.error("CTD reverse lookup: Supabase client unavailable: %s", exc)
        return []

    columns = "chemical_name,gene_symbol,gene_id,interaction_actions,pubmed_ids,publication_count"
    rows: list[dict[str, Any]] = []
    matched_lower: set[str] = set()

    for batch_start in range(0, len(deduped), _MAX_CHEMICALS_PER_REQUEST):
        batch = deduped[batch_start : batch_start + _MAX_CHEMICALS_PER_REQUEST]
        try:
            resp = (
                await client.table(_TABLE)
                .select(columns)
                .in_("chemical_name", batch)
                .execute()
            )
        except Exception as exc:
            logger.error("CTD reverse Supabase query failed: %s", exc)
            continue
        for r in resp.data or []:
            rows.append(r)
            matched_lower.add((r.get("chemical_name") or "").strip().lower())

    # Case-insensitive fallback for any name the exact filter missed.
    misses = [n for n in deduped if n.lower() not in matched_lower]
    for name in misses:
        try:
            resp = (
                await client.table(_TABLE)
                .select(columns)
                .ilike("chemical_name", name)
                .execute()
            )
        except Exception as exc:
            logger.warning("CTD reverse ilike lookup failed for %r: %s", name, exc)
            continue
        rows.extend(resp.data or [])

    results = [_normalize_row(r) for r in rows if r]
    logger.info(
        "CTD reverse: %d gene interaction(s) found across %d phytochemical(s)",
        len(results),
        len(deduped),
    )
    return results


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map a Supabase row to the live-API response shape."""
    actions = row.get("interaction_actions") or []
    interaction_type = ""
    if actions:
        first = str(actions[0]) if actions[0] is not None else ""
        interaction_type = first.replace("^", " ").strip()

    pmids = list(row.get("pubmed_ids") or [])
    pub_count = row.get("publication_count")
    if pub_count is None:
        pub_count = len(pmids)

    return {
        "chemical_name": (row.get("chemical_name") or "").strip(),
        "gene_symbol": (row.get("gene_symbol") or "").strip(),
        "gene_id": (str(row.get("gene_id")).strip() if row.get("gene_id") else ""),
        "interaction_type": interaction_type,
        "publication_count": int(pub_count),
        "pmids": pmids,
    }


def _is_phytochemical(name: str) -> bool:
    """Whitelist match (substring, case-insensitive) OR class-pattern match."""
    lname = name.lower()
    if any(p in lname for p in _PHYTOCHEMICAL_NAMES):
        return True
    return bool(_PHYTOCHEMICAL_PATTERNS.search(lname))


def _normalize_interaction(raw: Any) -> str:
    """Convert CTD's 'decreases^expression|increases^activity' form to readable text.

    Returns the first action, e.g. 'decreases expression'. Falls back to the
    raw string for free-text Interaction fields.
    """
    if not isinstance(raw, str) or not raw:
        return ""
    first = raw.split("|", 1)[0]
    return first.replace("^", " ").strip()


def _split_pmids(value: Any) -> list[str]:
    """CTD returns PubMedIDs as a pipe-delimited string."""
    if not isinstance(value, str) or not value:
        return []
    return [pmid.strip() for pmid in value.split("|") if pmid.strip()]
