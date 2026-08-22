"""ChEMBL API integration — natural products only.

Docs: https://www.ebi.ac.uk/chembl/api/data/docs

The pipeline only wants food bioactives (quercetin, resveratrol, curcumin…),
not synthetic drugs or clinical candidates.  ChEMBL's natural_product flag
lives on the molecule record, so filtering requires two requests:
  1. Fetch activities for the target  → collect candidate molecule IDs
  2. Batch-query the molecule endpoint → keep only natural products
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import get_settings
from app.models import CompoundGeneInteraction

logger = logging.getLogger(__name__)

# Compounds in late-stage clinical trials or already approved are almost
# certainly pharmaceuticals, not dietary compounds.  Skip max_phase >= this.
_MAX_PHASE_CUTOFF = 2

# Fallback-path bounds. ChEMBL is unauthenticated and rate-limited, and this
# path only runs when CTD came back empty, so keep the fan-out modest.
_FALLBACK_MAX_GENES = 10
_FALLBACK_CONCURRENCY = 4
_FALLBACK_ACTIVITIES_PER_GENE = 200
_FALLBACK_MAX_COMPOUNDS_PER_GENE = 25


def _base_url() -> str:
    return get_settings().chembl_api_url.rstrip("/")


async def get_compounds_for_gene(
    gene_symbol: str,
    gene_id: str,
    limit: int = 20,
) -> list[CompoundGeneInteraction]:
    """Fetch natural-product bioactive compounds targeting a gene from ChEMBL.

    Two-step process:
      1. Resolve gene symbol → ChEMBL target ID
      2. Fetch activities, then filter to natural products via molecule endpoint
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        target_chembl_id = await _resolve_target(client, gene_symbol)
        if not target_chembl_id:
            return []

        candidate_activities = await _fetch_activities(client, target_chembl_id, fetch_limit=limit * 3)
        if not candidate_activities:
            return []

        natural_ids = await _filter_natural_products(
            client,
            molecule_ids=[a["molecule_chembl_id"] for a in candidate_activities],
        )

    interactions: list[CompoundGeneInteraction] = []
    for act in candidate_activities:
        if len(interactions) >= limit:
            break

        compound_id: str = act["molecule_chembl_id"]
        if compound_id not in natural_ids:
            logger.debug("Skipping non-natural compound %s", compound_id)
            continue

        compound_name: str = natural_ids[compound_id]
        try:
            interactions.append(
                CompoundGeneInteraction(
                    compound_id=compound_id,
                    compound_name=compound_name,
                    gene_id=gene_id,
                    gene_symbol=gene_symbol,
                    activity_type=act.get("standard_type"),
                    activity_value=_safe_float(act.get("standard_value")),
                    activity_units=act.get("standard_units"),
                    source="chembl",
                )
            )
        except Exception:
            logger.warning("Skipping malformed ChEMBL activity record: %s", act)

    logger.info(
        "ChEMBL: %d natural-product interaction(s) found for gene %s (from %d candidates)",
        len(interactions),
        gene_symbol,
        len(candidate_activities),
    )
    return interactions


async def get_natural_compounds_for_genes(
    gene_symbols: list[str],
) -> list[dict[str, Any]]:
    """Fallback compound lookup for when the CTD snapshot returns nothing.

    Returns rows in exactly the shape `ctd_api.get_chemicals_for_genes` emits
    (chemical_name, gene_symbol, gene_id, interaction_type, publication_count,
    pmids) plus a `source` key, so `chain_builder` can merge the two without
    special-casing either. PMIDs are resolved from the ChEMBL documents backing
    each activity — without them the recommendation would surface with no
    citations to click.

    Never raises: a fallback that breaks the request is worse than no fallback.
    """
    genes = [g.strip().upper() for g in gene_symbols if (g or "").strip()]
    # Deduplicate, preserving the caller's ordering (already ranked by score).
    seen: set[str] = set()
    ordered = [g for g in genes if not (g in seen or seen.add(g))]
    if not ordered:
        return []

    capped = ordered[:_FALLBACK_MAX_GENES]
    semaphore = asyncio.Semaphore(_FALLBACK_CONCURRENCY)

    async with httpx.AsyncClient(timeout=30.0) as client:

        async def one(gene: str) -> list[dict[str, Any]]:
            async with semaphore:
                try:
                    return await _natural_compounds_for_gene(client, gene)
                except Exception as exc:  # noqa: BLE001 - fallback must not raise
                    logger.warning("ChEMBL fallback failed for gene %s: %s", gene, exc)
                    return []

        batches = await asyncio.gather(*(one(g) for g in capped))

    rows = [row for batch in batches for row in batch]
    logger.info(
        "ChEMBL fallback: %d interaction row(s) across %d/%d gene(s)",
        len(rows),
        len(capped),
        len(ordered),
    )
    return rows


async def _natural_compounds_for_gene(
    client: httpx.AsyncClient,
    gene_symbol: str,
) -> list[dict[str, Any]]:
    """CTD-shaped natural-product rows for one gene."""
    target_id = await _resolve_target(client, gene_symbol)
    if not target_id:
        return []

    activities = await _fetch_activities_raw(
        client, target_id, fetch_limit=_FALLBACK_ACTIVITIES_PER_GENE
    )
    if not activities:
        return []

    molecule_ids = list({a["molecule_chembl_id"] for a in activities})
    natural = await _filter_natural_products(client, molecule_ids=molecule_ids)
    if not natural:
        return []

    # A recommendation card reading "CHEMBL127042" is worse than no card, and
    # ChEMBL leaves pref_name empty on most of its research compounds.
    natural = {
        mid: name
        for mid, name in natural.items()
        if name and not _looks_like_chembl_id(name)
    }
    if not natural:
        logger.info("ChEMBL: no named natural compounds for gene %s", gene_symbol)
        return []

    # Group each natural compound's supporting documents and activity types.
    by_molecule: dict[str, dict[str, Any]] = {}
    for act in activities:
        mid = act.get("molecule_chembl_id")
        if mid not in natural:
            continue
        entry = by_molecule.setdefault(mid, {"documents": set(), "types": []})
        doc_id = act.get("document_chembl_id")
        if doc_id:
            entry["documents"].add(doc_id)
        std_type = (act.get("standard_type") or "").strip()
        if std_type and std_type not in entry["types"]:
            entry["types"].append(std_type)

    if not by_molecule:
        return []

    # Rank by how much literature backs the pair, then resolve PMIDs for the
    # documents we will actually cite.
    ranked = sorted(
        by_molecule.items(), key=lambda kv: len(kv[1]["documents"]), reverse=True
    )[:_FALLBACK_MAX_COMPOUNDS_PER_GENE]

    all_docs = {doc for _, entry in ranked for doc in entry["documents"]}
    pmid_by_doc = await _resolve_pmids(client, sorted(all_docs))

    rows: list[dict[str, Any]] = []
    for mid, entry in ranked:
        pmids = sorted({pmid_by_doc[d] for d in entry["documents"] if d in pmid_by_doc})
        rows.append(
            {
                "chemical_name": natural[mid],
                "gene_symbol": gene_symbol,
                "gene_id": "",
                "interaction_type": _interaction_phrase(entry["types"]),
                # Count the documents, not just the ones with a PubMed ID —
                # a ChEMBL document without a PMID is still a distinct source.
                "publication_count": len(entry["documents"]),
                "pmids": pmids,
                "source": "ChEMBL",
            }
        )
    return rows


async def _fetch_activities_raw(
    client: httpx.AsyncClient,
    target_chembl_id: str,
    fetch_limit: int,
) -> list[dict[str, Any]]:
    """Every activity row for a target, un-deduplicated.

    Unlike `_fetch_activities`, this keeps repeated molecules: the repetition
    is the signal used to count supporting documents per compound.
    """
    url = f"{_base_url()}/activity.json"
    params = {
        "target_chembl_id": target_chembl_id,
        "limit": fetch_limit,
        "format": "json",
    }
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    activities: list[Any] = resp.json().get("activities", [])
    return [
        a
        for a in activities
        if isinstance(a, dict) and a.get("molecule_chembl_id")
    ]


async def _resolve_pmids(
    client: httpx.AsyncClient,
    document_ids: list[str],
) -> dict[str, str]:
    """Map ChEMBL document IDs to PubMed IDs, in chunks the API will accept."""
    if not document_ids:
        return {}

    out: dict[str, str] = {}
    chunk_size = 50
    for start in range(0, len(document_ids), chunk_size):
        chunk = document_ids[start : start + chunk_size]
        try:
            resp = await client.get(
                f"{_base_url()}/document.json",
                params={
                    "document_chembl_id__in": ",".join(chunk),
                    "limit": len(chunk),
                    "format": "json",
                },
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - citations are best-effort
            logger.warning("ChEMBL document lookup failed: %s", exc)
            continue

        for doc in resp.json().get("documents", []):
            if not isinstance(doc, dict):
                continue
            doc_id = doc.get("document_chembl_id")
            pubmed_id = doc.get("pubmed_id")
            if doc_id and pubmed_id:
                out[doc_id] = str(pubmed_id)
    return out


def _interaction_phrase(activity_types: list[str]) -> str:
    """Describe a ChEMBL bioactivity in the same register as CTD's phrasing.

    CTD says things like "decreases expression"; ChEMBL reports assay endpoints
    (IC50, Ki, EC50). Both describe a measured interaction, so we render the
    endpoint as binding/activity language the UI already knows how to label.
    """
    if not activity_types:
        return "binding"
    primary = activity_types[0].upper()
    if primary in {"IC50", "KI", "KD", "POTENCY", "INHIBITION"}:
        return f"binding ({activity_types[0]})"
    if primary in {"EC50", "AC50", "ACTIVITY"}:
        return f"activity ({activity_types[0]})"
    return f"binding ({activity_types[0]})"


async def _resolve_target(client: httpx.AsyncClient, gene_symbol: str) -> str | None:
    """Return the first matching ChEMBL target ID for a gene symbol."""
    url = f"{_base_url()}/target.json"
    params = {
        "target_synonym__icontains": gene_symbol,
        "target_type": "SINGLE PROTEIN",
        "organism": "Homo sapiens",
        "limit": 5,
        "format": "json",
    }
    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "ChEMBL target lookup failed for %s [%s]: %s",
            gene_symbol,
            exc.response.status_code,
            exc.response.text,
        )
        raise

    # ChEMBL envelope: {"targets": [...], "page_meta": {...}}
    targets: list[Any] = resp.json().get("targets", [])
    if not targets:
        logger.info("No ChEMBL targets found for gene %s", gene_symbol)
        return None
    return targets[0].get("target_chembl_id", "")


async def _fetch_activities(
    client: httpx.AsyncClient,
    target_chembl_id: str,
    fetch_limit: int,
) -> list[dict[str, Any]]:
    """Fetch activity records for a target, returning only rows with a molecule name."""
    url = f"{_base_url()}/activity.json"
    params = {
        "target_chembl_id": target_chembl_id,
        "limit": fetch_limit,
        "format": "json",
    }
    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "ChEMBL activity lookup failed for target %s [%s]: %s",
            target_chembl_id,
            exc.response.status_code,
            exc.response.text,
        )
        raise

    # ChEMBL envelope: {"activities": [...], "page_meta": {...}}
    activities: list[Any] = resp.json().get("activities", [])

    # Deduplicate by molecule ID and require a preferred name.
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for act in activities:
        mid = act.get("molecule_chembl_id")
        if not mid or mid in seen:
            continue
        if not act.get("molecule_pref_name"):
            logger.debug("Skipping %s — no preferred name", mid)
            continue
        seen.add(mid)
        result.append(act)

    return result


async def _filter_natural_products(
    client: httpx.AsyncClient,
    molecule_ids: list[str],
) -> dict[str, str]:
    """Fetch molecule records and return {chembl_id: pref_name} for natural products.

    Filtering is done client-side after fetching, because the ChEMBL molecule
    endpoint rejects molecule_properties__natural_product and max_phase__lt as
    query parameters with a 400 error.

    Rules (each field is checked only when present in the response):
      - molecule_properties.natural_product == 1  → keep
      - molecule_properties.natural_product == 0  → skip (confirmed synthetic)
      - field absent                              → accept (data not available)
      - max_phase >= _MAX_PHASE_CUTOFF            → skip (late-stage drug)
      - max_phase < _MAX_PHASE_CUTOFF or None     → accept
    """
    if not molecule_ids:
        return {}

    url = f"{_base_url()}/molecule.json"
    params = {
        "molecule_chembl_id__in": ",".join(molecule_ids),
        "limit": len(molecule_ids),
        "format": "json",
    }
    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "ChEMBL molecule fetch failed [%s]: %s",
            exc.response.status_code,
            exc.response.text,
        )
        raise

    # ChEMBL envelope: {"molecules": [...], "page_meta": {...}}
    molecules: list[Any] = resp.json().get("molecules", [])

    result: dict[str, str] = {}
    for m in molecules:
        if not isinstance(m, dict):
            continue
        chembl_id: str | None = m.get("molecule_chembl_id")
        if not chembl_id:
            continue

        # --- natural_product flag ---
        props: dict[str, Any] = m.get("molecule_properties") or {}
        np_flag = props.get("natural_product")
        if np_flag is not None and int(np_flag) != 1:
            logger.debug("Skipping %s — confirmed synthetic (natural_product=%s)", chembl_id, np_flag)
            continue

        # --- max_phase filter ---
        max_phase = m.get("max_phase")
        if max_phase is not None:
            try:
                if float(max_phase) >= _MAX_PHASE_CUTOFF:
                    logger.debug("Skipping %s — max_phase=%s", chembl_id, max_phase)
                    continue
            except (TypeError, ValueError):
                pass  # unparseable max_phase → accept

        result[chembl_id] = _display_name(m) or chembl_id

    logger.debug(
        "Natural product filter: %d/%d molecules passed", len(result), len(molecules)
    )
    return result


def _display_name(molecule: dict[str, Any]) -> str:
    """Best human-readable name for a molecule.

    ChEMBL leaves `pref_name` null on most research compounds, so fall back to
    the synonym list before giving up.
    """
    pref = (molecule.get("pref_name") or "").strip()
    if pref:
        return pref.title() if pref.isupper() else pref

    for syn in molecule.get("molecule_synonyms") or []:
        if not isinstance(syn, dict):
            continue
        name = (syn.get("molecule_synonym") or "").strip()
        # Registry codes ("NSC-760125") are no more readable than the ID itself.
        if name and not _looks_like_registry_code(name):
            return name.title() if name.isupper() else name
    return ""


def _looks_like_chembl_id(name: str) -> bool:
    return name.strip().upper().startswith("CHEMBL")


def _looks_like_registry_code(name: str) -> bool:
    """True for catalogue identifiers such as NSC-760125 or SID 12345."""
    stripped = name.strip().upper()
    if _looks_like_chembl_id(stripped):
        return True
    head, _, tail = stripped.partition("-")
    return bool(tail) and head.isalpha() and len(head) <= 4 and tail.isdigit()


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
