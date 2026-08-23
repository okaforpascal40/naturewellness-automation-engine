"""Curated host-response gene sets for infections Open Targets covers poorly.

Design note — this map supplies GENES, not compounds.

The obvious shape would be to hardcode a compound list per disease and return
it. We deliberately do not: a hardcoded compound carries no citation, so it
would surface in the app with an empty References tab, and the whole point of
the evidence UI is that a reader can check every claim. Instead each disease
maps to host-response genes that are genuinely studied for it, and the normal
pipeline runs from there — CTD supplies the compound-gene interactions and the
PMIDs behind them. The only curated link in the chain is disease -> gene, and
that is what `evidence_note` discloses.

`typical_compounds` is advisory: it records which dietary compounds the
literature most often pairs with the condition, and is used only to rank CTD
results, never to invent one.

Gene symbols are HGNC-approved (NFKB1, not "NF-KB") or the CTD lookup silently
finds nothing.
"""
from __future__ import annotations

from typing import Any, TypedDict


class InfectiousDiseaseEntry(TypedDict):
    genes: list[str]
    typical_compounds: list[str]
    mechanism: str
    evidence_note: str


# Every infection here can kill and every one has a real medical treatment.
# Attached to any response built from this map.
TREATMENT_DISCLAIMER = (
    "These food suggestions are based on general phytochemical literature for "
    "this type of infection, not on a gene-pathway analysis of your condition. "
    "They are not a treatment and not a substitute for medical care — "
    "infections of this kind require diagnosis and treatment by a clinician."
)

GENERIC_EVIDENCE_NOTE = (
    "Open Targets has no strongly-scored gene associations for this condition, "
    "so results are built from curated host-response genes for this infection "
    "type rather than a disease-specific gene analysis."
)

INFECTIOUS_DISEASE_MAP: dict[str, InfectiousDiseaseEntry] = {
    "typhoid": {
        "genes": ["TNF", "IL6", "NFKB1", "TLR4", "IL10", "IFNG"],
        "typical_compounds": ["Quercetin", "Curcumin", "Berberine", "Allicin", "Thymoquinone"],
        "mechanism": "antimicrobial and immune-modulating properties",
        "evidence_note": "Based on antimicrobial phytochemical literature",
    },
    "malaria": {
        "genes": ["TNF", "IL6", "HBB", "G6PD", "TLR9", "ICAM1"],
        # Artemisinin is deliberately absent. It is a frontline antimalarial,
        # and WHO advises against Artemisia annua food or herbal preparations:
        # sub-therapeutic dosing drives artemisinin resistance. Listing it as a
        # dietary suggestion for malaria would be actively harmful.
        "typical_compounds": ["Quercetin", "Curcumin", "Resveratrol", "Epigallocatechin gallate"],
        "mechanism": "immune-supporting and anti-inflammatory properties",
        "evidence_note": "Based on nutritional support literature for malaria",
    },
    "tuberculosis": {
        "genes": ["TNF", "IL6", "VDR", "NOD2", "TLR2", "IFNG"],
        "typical_compounds": ["Berberine", "Quercetin", "Curcumin", "Allicin", "Resveratrol"],
        "mechanism": "antimycobacterial and immune-modulating properties",
        "evidence_note": "Based on antimycobacterial phytochemical literature",
    },
    "cholera": {
        "genes": ["TNF", "IL6", "CFTR", "AQP1", "TLR4", "NFKB1"],
        "typical_compounds": ["Quercetin", "Berberine", "Allicin", "Curcumin"],
        "mechanism": "antimicrobial and gut-protective properties",
        "evidence_note": "Based on antimicrobial phytochemical literature",
    },
    "dengue": {
        "genes": ["TNF", "IL6", "STAT3", "IRF3", "CD209", "IL10"],
        "typical_compounds": ["Quercetin", "Curcumin", "Resveratrol", "Fisetin"],
        "mechanism": "antiviral and anti-inflammatory properties",
        "evidence_note": "Based on antiviral phytochemical literature",
    },
    "covid": {
        "genes": ["TNF", "IL6", "ACE2", "TMPRSS2", "STAT3", "IL6R"],
        "typical_compounds": [
            "Quercetin",
            "Resveratrol",
            "Curcumin",
            "Epigallocatechin gallate",
            "Berberine",
        ],
        "mechanism": "antiviral, anti-inflammatory and immune-modulating properties",
        "evidence_note": "Based on COVID-19 phytochemical literature",
    },
    "hepatitis": {
        "genes": ["TNF", "IL6", "TP53", "CASP3", "BCL2", "IFNAR1"],
        "typical_compounds": ["Silymarin", "Curcumin", "Quercetin", "Resveratrol", "Berberine"],
        "mechanism": "hepatoprotective and antiviral properties",
        "evidence_note": "Based on hepatoprotective phytochemical literature",
    },
    "pneumonia": {
        "genes": ["TNF", "IL6", "TLR4", "NFKB1", "STAT3", "CXCL8"],
        "typical_compounds": [
            "Quercetin",
            "Curcumin",
            "Berberine",
            "Epigallocatechin gallate",
            "Allicin",
        ],
        "mechanism": "antimicrobial and anti-inflammatory properties",
        "evidence_note": "Based on respiratory infection phytochemical literature",
    },
    "hiv": {
        "genes": ["TNF", "IL6", "CCR5", "CXCR4", "CD4", "STAT3"],
        "typical_compounds": ["Quercetin", "Resveratrol", "Curcumin", "Silymarin", "Berberine"],
        "mechanism": "immune-supporting and anti-inflammatory properties",
        "evidence_note": "Based on nutritional support literature for HIV",
    },
    "malnutrition": {
        "genes": ["TNF", "IL6", "PPARG", "LEP", "IGF1", "ALB"],
        "typical_compounds": ["Quercetin", "Curcumin", "Beta-carotene", "Anthocyanins"],
        "mechanism": "nutrient-dense and antioxidant properties",
        "evidence_note": "Based on nutritional phytochemical literature",
    },
}


def lookup(disease_name: str) -> tuple[str, InfectiousDiseaseEntry] | None:
    """Find the curated entry whose key appears in the disease name.

    Longest key first so "hepatitis" cannot shadow a more specific future key.
    """
    lowered = (disease_name or "").strip().lower()
    if not lowered:
        return None
    for key in sorted(INFECTIOUS_DISEASE_MAP, key=len, reverse=True):
        if key in lowered:
            return key, INFECTIOUS_DISEASE_MAP[key]
    return None


def response_metadata(entry: InfectiousDiseaseEntry) -> dict[str, Any]:
    """The provenance block attached to any run built from this map."""
    return {
        "data_source": "curated_literature",
        "evidence_note": f"{entry['evidence_note']} — {entry['mechanism']}. {GENERIC_EVIDENCE_NOTE}",
        "disclaimer": TREATMENT_DISCLAIMER,
    }
