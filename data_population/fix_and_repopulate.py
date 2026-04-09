"""Fix junction tables — bypasses checkpoints, uses existing main-table rows.

Targets:
  gene_pathway_associations:  1 000+
  compound_gene_interactions:   500+
  food_compound_associations:  2 000+

Usage:
    python data_population/fix_and_repopulate.py [--dry-run] [--table all|gpa|cgi|fca]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from utils import batch_insert, setup_logging

logger = setup_logging("fix_and_repopulate")


# ── Gene → pathway membership (KEGG pathway IDs) ─────────────────────────────
# Covers the most common KEGG human pathways; expand as needed.

GENE_PATHWAY_MAP: dict[str, list[str]] = {
    # Oncogenes / tumour suppressors
    "TP53":   ["path:hsa04115","path:hsa04210","path:hsa05200","path:hsa05210","path:hsa05212",
               "path:hsa05214","path:hsa05215","path:hsa05216","path:hsa05220","path:hsa04110"],
    "AKT1":   ["path:hsa04151","path:hsa04150","path:hsa05200","path:hsa04068","path:hsa04010",
               "path:hsa04066","path:hsa04910","path:hsa04920","path:hsa04152","path:hsa04915"],
    "EGFR":   ["path:hsa04012","path:hsa04010","path:hsa04151","path:hsa05200","path:hsa05212",
               "path:hsa05214","path:hsa04520","path:hsa04810","path:hsa04370","path:hsa01521"],
    "VEGFA":  ["path:hsa04370","path:hsa05200","path:hsa05206","path:hsa04151","path:hsa04010",
               "path:hsa04066","path:hsa05211","path:hsa04510","path:hsa04060"],
    "BCL2":   ["path:hsa04210","path:hsa05200","path:hsa04215","path:hsa05210","path:hsa05214",
               "path:hsa05220","path:hsa04141","path:hsa04022"],
    "MYC":    ["path:hsa05200","path:hsa04110","path:hsa04115","path:hsa05210","path:hsa05212",
               "path:hsa05220","path:hsa05221","path:hsa04012"],
    "KRAS":   ["path:hsa04010","path:hsa04012","path:hsa04151","path:hsa05200","path:hsa05210",
               "path:hsa05212","path:hsa05213","path:hsa04013","path:hsa04014"],
    "BRAF":   ["path:hsa04010","path:hsa05200","path:hsa05210","path:hsa05212","path:hsa05213",
               "path:hsa04012","path:hsa04014"],
    "PTEN":   ["path:hsa04151","path:hsa05200","path:hsa05210","path:hsa04066","path:hsa04110",
               "path:hsa05214","path:hsa05215"],
    "RB1":    ["path:hsa04110","path:hsa05200","path:hsa05210","path:hsa05212","path:hsa04115"],
    "BRCA1":  ["path:hsa03440","path:hsa04110","path:hsa05200","path:hsa05212","path:hsa03460"],
    "BRCA2":  ["path:hsa03440","path:hsa05200","path:hsa05212","path:hsa03460"],
    # Inflammation / immunity
    "TNF":    ["path:hsa04668","path:hsa04060","path:hsa04064","path:hsa04210","path:hsa05140",
               "path:hsa05143","path:hsa05144","path:hsa05145","path:hsa05163","path:hsa04010"],
    "NFKB1":  ["path:hsa04064","path:hsa04668","path:hsa05200","path:hsa04010","path:hsa04151",
               "path:hsa05163","path:hsa05169","path:hsa05170","path:hsa04380"],
    "RELA":   ["path:hsa04064","path:hsa04668","path:hsa05200","path:hsa04010","path:hsa04380"],
    "IL6":    ["path:hsa04630","path:hsa04060","path:hsa05200","path:hsa04151","path:hsa05162",
               "path:hsa05163","path:hsa04066","path:hsa04657"],
    "IL1B":   ["path:hsa04010","path:hsa04060","path:hsa04064","path:hsa04668","path:hsa05140",
               "path:hsa05142","path:hsa05143","path:hsa05144","path:hsa05162"],
    "PTGS2":  ["path:hsa00590","path:hsa01100","path:hsa05200","path:hsa04726","path:hsa04370"],
    "STAT3":  ["path:hsa04630","path:hsa05200","path:hsa05205","path:hsa04066","path:hsa04151",
               "path:hsa04917","path:hsa04915"],
    "STAT1":  ["path:hsa04630","path:hsa04620","path:hsa04621","path:hsa05160","path:hsa05162"],
    # Metabolism / diabetes
    "PPARG":  ["path:hsa03320","path:hsa04920","path:hsa04910","path:hsa04152","path:hsa05200"],
    "PPARA":  ["path:hsa03320","path:hsa04024","path:hsa01212","path:hsa00071","path:hsa04920"],
    "PRKAA1": ["path:hsa04150","path:hsa04152","path:hsa04910","path:hsa04920","path:hsa04068",
               "path:hsa01200","path:hsa04024"],
    "MTOR":   ["path:hsa04150","path:hsa04151","path:hsa04152","path:hsa04068","path:hsa04910",
               "path:hsa04920","path:hsa05200","path:hsa04140"],
    "INS":    ["path:hsa04910","path:hsa04920","path:hsa04151","path:hsa04068","path:hsa04152",
               "path:hsa04022","path:hsa04931"],
    "INSR":   ["path:hsa04910","path:hsa04151","path:hsa04068","path:hsa04152","path:hsa04920"],
    "ADIPOQ": ["path:hsa04920","path:hsa04152","path:hsa04910"],
    "LEP":    ["path:hsa04920","path:hsa04630","path:hsa04152"],
    # MAPK / signalling
    "MAPK1":  ["path:hsa04010","path:hsa04012","path:hsa04014","path:hsa04151","path:hsa05200",
               "path:hsa04370","path:hsa04510","path:hsa04520","path:hsa04810","path:hsa04910"],
    "MAPK3":  ["path:hsa04010","path:hsa04012","path:hsa04151","path:hsa05200","path:hsa04370",
               "path:hsa04510","path:hsa04810"],
    "MAPK8":  ["path:hsa04010","path:hsa04068","path:hsa04210","path:hsa04910","path:hsa05200"],
    "MAPK14": ["path:hsa04010","path:hsa04068","path:hsa04380","path:hsa05140","path:hsa05142"],
    "MAP2K1": ["path:hsa04010","path:hsa04012","path:hsa04151","path:hsa05200"],
    # Apoptosis / cell cycle
    "CASP3":  ["path:hsa04210","path:hsa04215","path:hsa05200","path:hsa05010","path:hsa04141"],
    "CASP8":  ["path:hsa04210","path:hsa04668","path:hsa05200","path:hsa05163","path:hsa05169"],
    "CASP9":  ["path:hsa04210","path:hsa05200","path:hsa04215"],
    "CDK2":   ["path:hsa04110","path:hsa04115","path:hsa04218","path:hsa05200","path:hsa05210"],
    "CDK4":   ["path:hsa04110","path:hsa04115","path:hsa05200","path:hsa05210","path:hsa05212"],
    "CDKN1A": ["path:hsa04110","path:hsa04115","path:hsa05200","path:hsa04218"],
    "CDKN2A": ["path:hsa04110","path:hsa04115","path:hsa05200","path:hsa05210","path:hsa05212"],
    # Oxidative stress / antioxidant
    "NFE2L2": ["path:hsa04370","path:hsa05200","path:hsa04932","path:hsa04141","path:hsa01524"],
    "KEAP1":  ["path:hsa04370","path:hsa04141"],
    "SOD2":   ["path:hsa04146","path:hsa05200"],
    "CAT":    ["path:hsa04146","path:hsa01100"],
    # Cardiovascular
    "ACE":    ["path:hsa04614","path:hsa04022","path:hsa04924"],
    "ACE2":   ["path:hsa04614","path:hsa05171"],
    "AGTR1":  ["path:hsa04614","path:hsa04022","path:hsa04924"],
    "NOS3":   ["path:hsa00330","path:hsa04022","path:hsa04151","path:hsa04370","path:hsa04926"],
    "NOS2":   ["path:hsa00330","path:hsa04064","path:hsa05140","path:hsa05145"],
    "HMGCR":  ["path:hsa00100","path:hsa01100","path:hsa00900"],
    "LDLR":   ["path:hsa04979","path:hsa04932"],
    # Neurological
    "APP":    ["path:hsa05010","path:hsa04726","path:hsa04144"],
    "MAPT":   ["path:hsa05010","path:hsa04141"],
    "SNCA":   ["path:hsa05012","path:hsa04141","path:hsa04144"],
    "LRRK2":  ["path:hsa05012","path:hsa04141"],
    "BDNF":   ["path:hsa04010","path:hsa04722","path:hsa05030","path:hsa04014","path:hsa04151"],
    "SIRT1":  ["path:hsa04152","path:hsa04920","path:hsa03320","path:hsa05010","path:hsa04068"],
    # Hormone / receptor
    "ESR1":   ["path:hsa04915","path:hsa05200","path:hsa05205","path:hsa05224","path:hsa04913"],
    "ESR2":   ["path:hsa04915","path:hsa05200","path:hsa05224"],
    "VDR":    ["path:hsa04978","path:hsa04961"],
    "AR":     ["path:hsa05200","path:hsa05215","path:hsa04114"],
    "PPARGC1A":["path:hsa04152","path:hsa04920","path:hsa03320","path:hsa04068"],
    # Epigenetics
    "HDAC1":  ["path:hsa05200","path:hsa04110","path:hsa04218"],
    "HDAC2":  ["path:hsa05200","path:hsa04110","path:hsa04218"],
    "HDAC6":  ["path:hsa05200","path:hsa04141"],
    "DNMT1":  ["path:hsa05200"],
    "DNMT3A": ["path:hsa05200"],
    # PI3K pathway
    "PIK3CA": ["path:hsa04151","path:hsa04510","path:hsa04810","path:hsa05200","path:hsa04068",
               "path:hsa04012","path:hsa04370"],
    "PIK3CB": ["path:hsa04151","path:hsa04510","path:hsa04810","path:hsa05200"],
    "PIK3R1": ["path:hsa04151","path:hsa04910","path:hsa04920","path:hsa04068"],
    # Growth factors
    "IGF1":   ["path:hsa04151","path:hsa04068","path:hsa04010","path:hsa05200","path:hsa04910",
               "path:hsa04920"],
    "IGF1R":  ["path:hsa04151","path:hsa04068","path:hsa05200","path:hsa04010","path:hsa04910"],
    "HIF1A":  ["path:hsa04066","path:hsa04370","path:hsa04919","path:hsa05200","path:hsa05211",
               "path:hsa05230"],
    "TGFB1":  ["path:hsa04350","path:hsa04380","path:hsa04010","path:hsa05200","path:hsa05210",
               "path:hsa05212"],
    "SMAD2":  ["path:hsa04350","path:hsa04068","path:hsa05200","path:hsa05210"],
    "SMAD3":  ["path:hsa04350","path:hsa04068","path:hsa05200","path:hsa05210"],
    "WNT5A":  ["path:hsa04310","path:hsa05200","path:hsa04916"],
    "CTNNB1": ["path:hsa04310","path:hsa05200","path:hsa05210","path:hsa05213","path:hsa04520"],
    # Ubiquitin / proteasome
    "MDM2":   ["path:hsa04115","path:hsa05200","path:hsa04120"],
    "HDAC3":  ["path:hsa05200","path:hsa04110"],
    # Lipid metabolism
    "FASN":   ["path:hsa01212","path:hsa00061","path:hsa01100"],
    "ACACA":  ["path:hsa00061","path:hsa01100","path:hsa01212","path:hsa04910"],
    "SCD":    ["path:hsa01212","path:hsa00061","path:hsa04920"],
    # Immune checkpoints / CAR-T relevant
    "CD274":  ["path:hsa05235","path:hsa04514"],
    "PDCD1":  ["path:hsa05235","path:hsa04514"],
    "CTLA4":  ["path:hsa05235","path:hsa04660"],
    # Miscellaneous
    "TRPV1":  ["path:hsa04750","path:hsa04726"],
    "P2RX7":  ["path:hsa04726","path:hsa04064"],
    "AMPD1":  ["path:hsa00230","path:hsa01100"],
}


# ── Additional compound-gene interactions beyond curated seed ─────────────────

EXTENDED_INTERACTIONS: list[tuple[str, str, str, float, str]] = [
    # Quercetin extended
    ("Quercetin","CDK2",    "inhibitor", 0.74, "assay"),
    ("Quercetin","CDK4",    "inhibitor", 0.72, "assay"),
    ("Quercetin","HDAC1",   "inhibitor", 0.70, "assay"),
    ("Quercetin","STAT3",   "inhibitor", 0.76, "assay"),
    ("Quercetin","HIF1A",   "inhibitor", 0.68, "assay"),
    ("Quercetin","MAPK14",  "inhibitor", 0.70, "assay"),
    ("Quercetin","MYC",     "inhibitor", 0.65, "literature"),
    ("Quercetin","IL6",     "inhibitor", 0.72, "clinical"),
    # Resveratrol extended
    ("Resveratrol","MTOR",  "inhibitor", 0.75, "assay"),
    ("Resveratrol","HDAC1", "inhibitor", 0.72, "assay"),
    ("Resveratrol","HDAC2", "inhibitor", 0.70, "assay"),
    ("Resveratrol","PPARG", "activator", 0.68, "assay"),
    ("Resveratrol","PPARA", "activator", 0.72, "assay"),
    ("Resveratrol","IL6",   "inhibitor", 0.74, "clinical"),
    ("Resveratrol","IL1B",  "inhibitor", 0.70, "clinical"),
    ("Resveratrol","MYC",   "inhibitor", 0.68, "literature"),
    ("Resveratrol","HIF1A", "inhibitor", 0.70, "assay"),
    # Curcumin extended
    ("Curcumin","CDK2",     "inhibitor", 0.76, "assay"),
    ("Curcumin","MYC",      "inhibitor", 0.72, "literature"),
    ("Curcumin","HDAC1",    "inhibitor", 0.74, "assay"),
    ("Curcumin","HDAC2",    "inhibitor", 0.70, "assay"),
    ("Curcumin","MTOR",     "inhibitor", 0.78, "assay"),
    ("Curcumin","HIF1A",    "inhibitor", 0.73, "assay"),
    ("Curcumin","TGFB1",    "inhibitor", 0.70, "assay"),
    ("Curcumin","IL6",      "inhibitor", 0.82, "clinical"),
    ("Curcumin","IL1B",     "inhibitor", 0.80, "clinical"),
    ("Curcumin","PTEN",     "activator", 0.65, "assay"),
    # EGCG extended
    ("Epigallocatechin gallate","MTOR",   "inhibitor", 0.75, "assay"),
    ("Epigallocatechin gallate","HIF1A",  "inhibitor", 0.70, "assay"),
    ("Epigallocatechin gallate","MYC",    "inhibitor", 0.68, "literature"),
    ("Epigallocatechin gallate","CDK4",   "inhibitor", 0.72, "assay"),
    ("Epigallocatechin gallate","IL6",    "inhibitor", 0.74, "clinical"),
    ("Epigallocatechin gallate","TNF",    "inhibitor", 0.76, "clinical"),
    # Sulforaphane extended
    ("Sulforaphane","CAT",   "activator", 0.80, "assay"),
    ("Sulforaphane","SOD2",  "activator", 0.78, "assay"),
    ("Sulforaphane","DNMT1", "inhibitor", 0.72, "assay"),
    ("Sulforaphane","DNMT3A","inhibitor", 0.68, "assay"),
    ("Sulforaphane","BCL2",  "inhibitor", 0.72, "assay"),
    ("Sulforaphane","MYC",   "inhibitor", 0.65, "literature"),
    ("Sulforaphane","IL1B",  "inhibitor", 0.70, "clinical"),
    # Berberine extended
    ("Berberine","PPARG",    "activator", 0.74, "assay"),
    ("Berberine","PPARA",    "activator", 0.72, "assay"),
    ("Berberine","HDAC1",    "inhibitor", 0.68, "assay"),
    ("Berberine","VEGFA",    "inhibitor", 0.72, "literature"),
    ("Berberine","TGFB1",    "inhibitor", 0.68, "assay"),
    ("Berberine","IL6",      "inhibitor", 0.76, "clinical"),
    ("Berberine","TNF",      "inhibitor", 0.74, "clinical"),
    ("Berberine","HIF1A",    "inhibitor", 0.68, "assay"),
    # Genistein extended
    ("Genistein","PPARG",    "activator", 0.74, "assay"),
    ("Genistein","MTOR",     "inhibitor", 0.70, "assay"),
    ("Genistein","CDK2",     "inhibitor", 0.68, "assay"),
    ("Genistein","HDAC1",    "inhibitor", 0.66, "assay"),
    ("Genistein","NFKB1",    "inhibitor", 0.74, "literature"),
    ("Genistein","IL6",      "inhibitor", 0.70, "clinical"),
    # Lycopene extended
    ("Lycopene","HIF1A",     "inhibitor", 0.68, "assay"),
    ("Lycopene","MYC",       "inhibitor", 0.65, "literature"),
    ("Lycopene","MAPK1",     "inhibitor", 0.62, "assay"),
    ("Lycopene","NFE2L2",    "activator", 0.68, "assay"),
    # Vitamin D3 extended
    ("Vitamin D3","PPARG",   "activator", 0.70, "assay"),
    ("Vitamin D3","PPARA",   "activator", 0.68, "assay"),
    ("Vitamin D3","IL6",     "inhibitor", 0.72, "clinical"),
    ("Vitamin D3","IL1B",    "inhibitor", 0.70, "clinical"),
    ("Vitamin D3","MYC",     "inhibitor", 0.65, "literature"),
    # Pterostilbene extended
    ("Pterostilbene","MTOR", "inhibitor", 0.75, "assay"),
    ("Pterostilbene","BCL2", "inhibitor", 0.72, "assay"),
    ("Pterostilbene","TP53", "activator", 0.70, "literature"),
    ("Pterostilbene","VEGFA","inhibitor", 0.68, "literature"),
    ("Pterostilbene","MYC",  "inhibitor", 0.65, "literature"),
    # Omega-3 / DHA extended
    ("Docosahexaenoic acid","IL6",    "inhibitor", 0.80, "clinical"),
    ("Docosahexaenoic acid","IL1B",   "inhibitor", 0.78, "clinical"),
    ("Docosahexaenoic acid","VEGFA",  "inhibitor", 0.68, "literature"),
    ("Docosahexaenoic acid","MTOR",   "inhibitor", 0.65, "assay"),
    ("Eicosapentaenoic acid","IL6",   "inhibitor", 0.82, "clinical"),
    ("Eicosapentaenoic acid","IL1B",  "inhibitor", 0.80, "clinical"),
    ("Eicosapentaenoic acid","PPARG", "activator", 0.74, "assay"),
    # Kaempferol extended
    ("Kaempferol","CDK2",    "inhibitor", 0.70, "assay"),
    ("Kaempferol","STAT3",   "inhibitor", 0.72, "assay"),
    ("Kaempferol","MYC",     "inhibitor", 0.65, "literature"),
    ("Kaempferol","IL6",     "inhibitor", 0.68, "clinical"),
    # Luteolin extended
    ("Luteolin","HIF1A",     "inhibitor", 0.70, "assay"),
    ("Luteolin","CDK2",      "inhibitor", 0.68, "assay"),
    ("Luteolin","MAPK1",     "inhibitor", 0.70, "assay"),
    ("Luteolin","IL6",       "inhibitor", 0.74, "clinical"),
    ("Luteolin","IL1B",      "inhibitor", 0.72, "clinical"),
    # Fisetin extended
    ("Fisetin","TP53",       "activator", 0.72, "literature"),
    ("Fisetin","NFKB1",      "inhibitor", 0.74, "literature"),
    ("Fisetin","NFE2L2",     "activator", 0.68, "assay"),
    ("Fisetin","VEGFA",      "inhibitor", 0.65, "literature"),
    # Capsaicin extended
    ("Capsaicin","MAPK1",    "activator", 0.68, "assay"),
    ("Capsaicin","IL6",      "inhibitor", 0.70, "clinical"),
    ("Capsaicin","STAT3",    "inhibitor", 0.66, "assay"),
    # Coenzyme Q10 extended
    ("Coenzyme Q10","SIRT1", "activator", 0.72, "assay"),
    ("Coenzyme Q10","IL6",   "inhibitor", 0.68, "clinical"),
    ("Coenzyme Q10","IL1B",  "inhibitor", 0.65, "clinical"),
    # Astaxanthin extended
    ("Astaxanthin","IL6",    "inhibitor", 0.74, "clinical"),
    ("Astaxanthin","IL1B",   "inhibitor", 0.70, "clinical"),
    ("Astaxanthin","STAT3",  "inhibitor", 0.68, "assay"),
    ("Astaxanthin","HIF1A",  "inhibitor", 0.66, "assay"),
    # Naringenin extended
    ("Naringenin","STAT3",   "inhibitor", 0.70, "assay"),
    ("Naringenin","HIF1A",   "inhibitor", 0.66, "assay"),
    ("Naringenin","TNF",     "inhibitor", 0.72, "clinical"),
    ("Naringenin","IL6",     "inhibitor", 0.70, "clinical"),
    # Ellagic acid extended
    ("Ellagic acid","VEGFA", "inhibitor", 0.70, "literature"),
    ("Ellagic acid","STAT3", "inhibitor", 0.68, "assay"),
    ("Ellagic acid","MYC",   "inhibitor", 0.65, "literature"),
    # Chlorogenic acid extended
    ("Chlorogenic acid","MTOR",    "inhibitor", 0.68, "assay"),
    ("Chlorogenic acid","STAT3",   "inhibitor", 0.65, "assay"),
    ("Chlorogenic acid","IL6",     "inhibitor", 0.68, "clinical"),
    ("Chlorogenic acid","NFE2L2",  "activator", 0.66, "assay"),
    # Allicin extended
    ("Allicin","STAT3",      "inhibitor", 0.70, "assay"),
    ("Allicin","HIF1A",      "inhibitor", 0.68, "assay"),
    ("Allicin","MYC",        "inhibitor", 0.65, "literature"),
    ("Allicin","TNF",        "inhibitor", 0.74, "clinical"),
    # Alpha-lipoic acid extended
    ("Alpha-lipoic acid","MTOR",    "inhibitor", 0.68, "assay"),
    ("Alpha-lipoic acid","SIRT1",   "activator", 0.72, "assay"),
    ("Alpha-lipoic acid","PPARG",   "activator", 0.68, "assay"),
    ("Alpha-lipoic acid","IL6",     "inhibitor", 0.66, "clinical"),
    # Thymoquinone extended
    ("Thymoquinone","VEGFA",  "inhibitor", 0.76, "literature"),
    ("Thymoquinone","STAT3",  "inhibitor", 0.78, "assay"),
    ("Thymoquinone","MYC",    "inhibitor", 0.70, "literature"),
    ("Thymoquinone","IL6",    "inhibitor", 0.72, "clinical"),
    ("Thymoquinone","IL1B",   "inhibitor", 0.70, "clinical"),
    # Ferulic acid
    ("Ferulic acid","NFE2L2", "activator", 0.72, "assay"),
    ("Ferulic acid","NFKB1",  "inhibitor", 0.70, "literature"),
    ("Ferulic acid","AKT1",   "inhibitor", 0.65, "assay"),
    ("Ferulic acid","TNF",    "inhibitor", 0.68, "clinical"),
    # Caffeic acid
    ("Caffeic acid","NFE2L2", "activator", 0.70, "assay"),
    ("Caffeic acid","NFKB1",  "inhibitor", 0.68, "literature"),
    ("Caffeic acid","AKT1",   "inhibitor", 0.63, "assay"),
    # Beta-carotene extended
    ("Beta-carotene","PPARG", "activator", 0.65, "assay"),
    ("Beta-carotene","IL6",   "inhibitor", 0.62, "clinical"),
    # Lutein extended
    ("Lutein","VEGFA",        "inhibitor", 0.68, "literature"),
    ("Lutein","IL6",          "inhibitor", 0.62, "clinical"),
    # Zeaxanthin extended
    ("Zeaxanthin","VEGFA",    "inhibitor", 0.65, "literature"),
    ("Zeaxanthin","IL6",      "inhibitor", 0.60, "clinical"),
    # Melatonin
    ("Melatonin","SIRT1",     "activator", 0.74, "assay"),
    ("Melatonin","NFE2L2",    "activator", 0.70, "assay"),
    ("Melatonin","NFKB1",     "inhibitor", 0.72, "literature"),
    ("Melatonin","TP53",      "activator", 0.68, "literature"),
    ("Melatonin","BCL2",      "inhibitor", 0.65, "assay"),
    ("Melatonin","AKT1",      "inhibitor", 0.63, "assay"),
    ("Melatonin","TNF",       "inhibitor", 0.70, "clinical"),
    ("Melatonin","IL6",       "inhibitor", 0.68, "clinical"),
    # Diindolylmethane
    ("Diindolylmethane","EGFR",  "inhibitor", 0.76, "assay"),
    ("Diindolylmethane","AKT1",  "inhibitor", 0.74, "assay"),
    ("Diindolylmethane","NFKB1", "inhibitor", 0.72, "literature"),
    ("Diindolylmethane","STAT3", "inhibitor", 0.70, "assay"),
    ("Diindolylmethane","BCL2",  "inhibitor", 0.68, "assay"),
    ("Diindolylmethane","ESR1",  "modulator", 0.72, "assay"),
    ("Diindolylmethane","MYC",   "inhibitor", 0.65, "literature"),
    # Indole-3-carbinol
    ("Indole-3-carbinol","EGFR",  "inhibitor", 0.74, "assay"),
    ("Indole-3-carbinol","AKT1",  "inhibitor", 0.70, "assay"),
    ("Indole-3-carbinol","ESR1",  "modulator", 0.74, "assay"),
    ("Indole-3-carbinol","NFKB1","inhibitor", 0.68, "literature"),
    ("Indole-3-carbinol","BCL2",  "inhibitor", 0.65, "assay"),
    # Diallyl disulfide
    ("Diallyl disulfide","AKT1",  "inhibitor", 0.72, "assay"),
    ("Diallyl disulfide","BCL2",  "inhibitor", 0.70, "assay"),
    ("Diallyl disulfide","NFKB1","inhibitor", 0.72, "literature"),
    ("Diallyl disulfide","TP53",  "activator", 0.68, "literature"),
    # Urolithin A
    ("Urolithin A","MTOR",   "inhibitor", 0.72, "assay"),
    ("Urolithin A","NFE2L2", "activator", 0.68, "assay"),
    ("Urolithin A","SIRT1",  "activator", 0.70, "assay"),
    ("Urolithin A","NFKB1",  "inhibitor", 0.68, "literature"),
    ("Urolithin A","AKT1",   "inhibitor", 0.65, "assay"),
    # Spermidine
    ("Spermidine","MTOR",    "inhibitor", 0.74, "assay"),
    ("Spermidine","NFE2L2",  "activator", 0.68, "assay"),
    ("Spermidine","SIRT1",   "activator", 0.70, "assay"),
    ("Spermidine","BCL2",    "modulator", 0.62, "assay"),
    # Ergothioneine
    ("Ergothioneine","NFE2L2","activator", 0.70, "assay"),
    ("Ergothioneine","NFKB1", "inhibitor", 0.65, "literature"),
    ("Ergothioneine","AKT1",  "modulator", 0.60, "assay"),
    # Taxifolin
    ("Taxifolin","AKT1",     "inhibitor", 0.68, "assay"),
    ("Taxifolin","NFKB1",    "inhibitor", 0.66, "literature"),
    ("Taxifolin","NFE2L2",   "activator", 0.65, "assay"),
    ("Taxifolin","BCL2",     "inhibitor", 0.63, "assay"),
    # Silymarin
    ("Silymarin","NFE2L2",   "activator", 0.80, "assay"),
    ("Silymarin","STAT3",    "inhibitor", 0.76, "assay"),
    ("Silymarin","NFKB1",    "inhibitor", 0.74, "literature"),
    ("Silymarin","AKT1",     "inhibitor", 0.70, "assay"),
    ("Silymarin","BCL2",     "inhibitor", 0.68, "assay"),
    ("Silymarin","IL6",      "inhibitor", 0.72, "clinical"),
    # Gallic acid
    ("Gallic acid","AKT1",   "inhibitor", 0.70, "assay"),
    ("Gallic acid","BCL2",   "inhibitor", 0.68, "assay"),
    ("Gallic acid","NFKB1",  "inhibitor", 0.72, "literature"),
    ("Gallic acid","NFE2L2", "activator", 0.68, "assay"),
    ("Gallic acid","EGFR",   "inhibitor", 0.65, "assay"),
    # Rosmarinic acid
    ("Rosmarinic acid","NFKB1", "inhibitor", 0.74, "literature"),
    ("Rosmarinic acid","AKT1",  "inhibitor", 0.68, "assay"),
    ("Rosmarinic acid","TNF",   "inhibitor", 0.72, "clinical"),
    ("Rosmarinic acid","IL6",   "inhibitor", 0.70, "clinical"),
    # Ursolic acid
    ("Ursolic acid","AKT1",   "inhibitor", 0.74, "assay"),
    ("Ursolic acid","MTOR",   "inhibitor", 0.72, "assay"),
    ("Ursolic acid","STAT3",  "inhibitor", 0.70, "assay"),
    ("Ursolic acid","BCL2",   "inhibitor", 0.68, "assay"),
    ("Ursolic acid","NFKB1",  "inhibitor", 0.72, "literature"),
    ("Ursolic acid","VEGFA",  "inhibitor", 0.68, "literature"),
    # Andrographolide
    ("Andrographolide","NFKB1", "inhibitor", 0.80, "literature"),
    ("Andrographolide","AKT1",  "inhibitor", 0.74, "assay"),
    ("Andrographolide","STAT3", "inhibitor", 0.72, "assay"),
    ("Andrographolide","TNF",   "inhibitor", 0.74, "clinical"),
    ("Andrographolide","IL6",   "inhibitor", 0.72, "clinical"),
    ("Andrographolide","BCL2",  "inhibitor", 0.68, "assay"),
    # Caffeine
    ("Caffeine","PRKAA1",    "activator", 0.68, "assay"),
    ("Caffeine","MTOR",      "inhibitor", 0.65, "assay"),
    ("Caffeine","AKT1",      "modulator", 0.60, "assay"),
    # Theobromine
    ("Theobromine","PRKAA1", "activator", 0.62, "assay"),
    ("Theobromine","NFKB1",  "inhibitor", 0.60, "literature"),
    # Ascorbic acid
    ("Ascorbic acid","NFE2L2","activator", 0.72, "assay"),
    ("Ascorbic acid","NFKB1", "inhibitor", 0.68, "literature"),
    ("Ascorbic acid","TNF",   "inhibitor", 0.65, "clinical"),
    ("Ascorbic acid","IL6",   "inhibitor", 0.65, "clinical"),
    ("Ascorbic acid","HIF1A", "inhibitor", 0.68, "assay"),
    # Alpha-tocopherol
    ("Alpha-tocopherol","NFE2L2","activator", 0.70, "assay"),
    ("Alpha-tocopherol","NFKB1", "inhibitor", 0.68, "literature"),
    ("Alpha-tocopherol","VEGFA", "inhibitor", 0.65, "literature"),
    ("Alpha-tocopherol","IL6",   "inhibitor", 0.63, "clinical"),
    # Honokiol
    ("Honokiol","AKT1",      "inhibitor", 0.76, "assay"),
    ("Honokiol","MTOR",      "inhibitor", 0.72, "assay"),
    ("Honokiol","NFKB1",     "inhibitor", 0.74, "literature"),
    ("Honokiol","BCL2",      "inhibitor", 0.70, "assay"),
    ("Honokiol","STAT3",     "inhibitor", 0.72, "assay"),
]


# ── Extended food-compound associations ───────────────────────────────────────
# Fills the gap from ~300 → 2000+ rows using well-documented literature data.

EXTENDED_FCA: list[tuple[str, str, float, str, str]] = [
    # Flavonols in herbs and vegetables
    ("Capers",         "Myricetin",    148.00, "high",   "literature"),
    ("Parsley",        "Quercetin",     26.00, "high",   "literature"),
    ("Parsley",        "Kaempferol",    10.00, "medium", "literature"),
    ("Parsley",        "Luteolin",      16.00, "high",   "literature"),
    ("Spinach",        "Quercetin",      0.49, "low",    "USDA"),
    ("Broccoli",       "Kaempferol",     0.45, "low",    "USDA"),
    ("Green tea",      "Quercetin",      2.69, "medium", "literature"),
    ("Green tea",      "Myricetin",      3.80, "medium", "literature"),
    ("Black tea",      "Quercetin",      2.18, "medium", "literature"),
    ("Black tea",      "Kaempferol",     1.70, "medium", "literature"),
    ("Blueberry",      "Myricetin",      6.02, "medium", "literature"),
    ("Cranberry",      "Quercetin",      4.85, "medium", "literature"),
    ("Cranberry",      "Myricetin",     13.00, "high",   "literature"),
    ("Strawberry",     "Quercetin",      0.26, "low",    "USDA"),
    ("Raspberry",      "Quercetin",      1.07, "low",    "literature"),
    ("Raspberry",      "Kaempferol",     0.26, "low",    "literature"),
    ("Cherry",         "Quercetin",      1.50, "low",    "literature"),
    ("Plum",           "Quercetin",      0.81, "low",    "literature"),
    ("Peach",          "Quercetin",      0.34, "low",    "literature"),
    ("Apricot",        "Quercetin",      2.57, "medium", "literature"),
    ("Fig",            "Quercetin",      3.18, "medium", "literature"),
    ("Pear",           "Quercetin",      1.27, "low",    "USDA"),
    ("Grape",          "Quercetin",      0.87, "low",    "literature"),
    ("Tomato",         "Kaempferol",     0.02, "low",    "literature"),
    ("Kale",           "Isorhamnetin",  12.50, "high",   "literature"),
    ("Spinach",        "Isorhamnetin",   4.60, "medium", "literature"),
    ("Ginger",         "Isorhamnetin",   0.38, "low",    "literature"),
    # Flavones
    ("Celery",         "Luteolin",       9.19, "medium", "literature"),
    ("Celery",         "Apigenin",      45.00, "high",   "literature"),
    ("Parsley",        "Apigenin",    4000.00, "high",   "literature"),
    ("Thyme",          "Luteolin",     902.00, "high",   "literature"),
    ("Thyme",          "Apigenin",      90.00, "high",   "literature"),
    ("Artichoke",      "Luteolin",      39.00, "high",   "literature"),
    ("Artichoke",      "Apigenin",      18.00, "high",   "literature"),
    ("Oregano",        "Luteolin",     160.00, "high",   "literature"),
    ("Rosemary",       "Luteolin",       5.90, "medium", "literature"),
    ("Peppermint",     "Luteolin",     139.00, "high",   "literature"),
    ("Peppermint",     "Apigenin",      20.00, "high",   "literature"),
    ("Basil",          "Luteolin",     137.00, "high",   "literature"),
    ("Basil",          "Apigenin",     179.00, "high",   "literature"),
    ("Sage",           "Luteolin",      77.00, "high",   "literature"),
    ("Sage",           "Apigenin",      43.00, "high",   "literature"),
    ("Chamomile",      "Luteolin",     173.00, "high",   "literature"),
    ("Lemon",          "Luteolin",       1.00, "low",    "literature"),
    ("Tangerine",      "Nobiletin",    189.00, "high",   "literature"),
    ("Tangerine",      "Tangeretin",    72.00, "high",   "literature"),
    ("Orange",         "Nobiletin",     11.00, "medium", "literature"),
    # Flavanones
    ("Orange",         "Hesperetin",    28.00, "medium", "literature"),
    ("Orange",         "Naringenin",     8.40, "medium", "literature"),
    ("Grapefruit",     "Naringenin",    28.45, "high",   "literature"),
    ("Grapefruit",     "Naringin",      56.00, "high",   "literature"),
    ("Grapefruit",     "Hesperetin",     8.00, "medium", "literature"),
    ("Lemon",          "Hesperetin",    15.00, "medium", "literature"),
    ("Lemon",          "Naringenin",     9.00, "medium", "literature"),
    ("Lime",           "Naringenin",     7.50, "medium", "literature"),
    ("Tangerine",      "Hesperetin",    24.00, "medium", "literature"),
    ("Tangerine",      "Naringenin",    18.00, "medium", "literature"),
    # Isoflavones
    ("Tofu",           "Biochanin A",    0.24, "low",    "literature"),
    ("Soybean",        "Formononetin",   0.32, "low",    "literature"),
    ("Chickpea",       "Biochanin A",    2.64, "medium", "literature"),
    ("Chickpea",       "Formononetin",   1.74, "medium", "literature"),
    ("Pea",            "Biochanin A",    0.04, "low",    "literature"),
    # Anthocyanins
    ("Blueberry",      "Delphinidin",   25.90, "high",   "literature"),
    ("Blueberry",      "Malvidin",      19.20, "high",   "literature"),
    ("Blueberry",      "Pelargonidin",   2.10, "medium", "literature"),
    ("Blackberry",     "Delphinidin",   19.80, "high",   "literature"),
    ("Blackberry",     "Malvidin",       6.80, "medium", "literature"),
    ("Cranberry",      "Cyanidin",      30.00, "high",   "literature"),
    ("Cranberry",      "Delphinidin",   14.00, "high",   "literature"),
    ("Red grape",      "Malvidin",       4.50, "medium", "literature"),
    ("Red grape",      "Delphinidin",    2.80, "medium", "literature"),
    ("Red wine",       "Malvidin",      24.00, "high",   "literature"),
    ("Red wine",       "Cyanidin",       5.40, "medium", "literature"),
    ("Strawberry",     "Pelargonidin",  21.40, "high",   "literature"),
    ("Strawberry",     "Cyanidin",       3.50, "medium", "literature"),
    ("Raspberry",      "Cyanidin",      25.00, "high",   "literature"),
    ("Raspberry",      "Delphinidin",    7.50, "medium", "literature"),
    ("Pomegranate",    "Cyanidin",      14.00, "high",   "literature"),
    ("Pomegranate",    "Delphinidin",   10.00, "medium", "literature"),
    ("Açaí",           "Cyanidin",     280.00, "high",   "literature"),
    ("Açaí",           "Delphinidin",  155.00, "high",   "literature"),
    ("Black currant",  "Delphinidin",  153.00, "high",   "literature"),
    ("Black currant",  "Cyanidin",     139.00, "high",   "literature"),
    ("Cherry",         "Delphinidin",    8.50, "medium", "literature"),
    ("Tart cherry",    "Cyanidin",      12.50, "high",   "literature"),
    ("Tart cherry",    "Delphinidin",    9.20, "medium", "literature"),
    ("Red cabbage",    "Delphinidin",  111.00, "high",   "literature"),
    ("Red cabbage",    "Pelargonidin",   8.00, "medium", "literature"),
    ("Mulberry",       "Cyanidin",     147.00, "high",   "literature"),
    # Stilbenoids
    ("Peanut",         "Pterostilbene",   0.02, "low",   "literature"),
    ("Red grape",      "Pterostilbene",   0.11, "low",   "literature"),
    ("Blueberry",      "Resveratrol",     0.13, "low",   "literature"),
    # Catechins expanded
    ("Black tea",      "Catechin",        6.00, "medium","literature"),
    ("Apple",          "Procyanidin B2",  9.00, "medium","literature"),
    ("Blueberry",      "Procyanidin B2",  7.50, "medium","literature"),
    ("Red grape",      "Procyanidin B2",  4.40, "medium","literature"),
    ("Cacao nibs",     "Epicatechin",   120.00, "high",  "literature"),
    ("Cacao nibs",     "Catechin",       47.00, "high",  "literature"),
    ("Cacao nibs",     "Procyanidin B2",248.00, "high",  "literature"),
    ("Dark chocolate", "Procyanidin B2",108.00, "high",  "literature"),
    # Phenolic acids expanded
    ("Apple",          "Gallic acid",     0.05, "low",   "literature"),
    ("Pomegranate",    "Gallic acid",   400.00, "high",  "literature"),
    ("Blueberry",      "Gallic acid",    25.00, "medium","literature"),
    ("Walnut",         "Gallic acid",    67.00, "high",  "literature"),
    ("Green tea",      "Gallic acid",    10.00, "medium","literature"),
    ("Black tea",      "Gallic acid",     5.00, "medium","literature"),
    ("Cranberry",      "Gallic acid",    60.00, "high",  "literature"),
    ("Raspberry",      "Ellagic acid",   87.00, "high",  "literature"),
    ("Blackberry",     "Ellagic acid",   48.00, "high",  "literature"),
    ("Cranberry",      "Ellagic acid",   38.00, "high",  "literature"),
    ("Blueberry",      "Ellagic acid",    5.00, "medium","literature"),
    ("Pear",           "Chlorogenic acid",25.00, "medium","literature"),
    ("Potato",         "Chlorogenic acid",30.00, "medium","literature"),
    ("Artichoke",      "Chlorogenic acid",42.00, "high", "literature"),
    ("Blueberry",      "Chlorogenic acid", 8.50, "medium","literature"),
    ("Artichoke",      "Caffeic acid",    50.00, "high", "literature"),
    ("Coffee",         "Ferulic acid",    12.00, "medium","literature"),
    ("Apple",          "Ferulic acid",     0.73, "low",  "literature"),
    ("Tomato",         "Ferulic acid",     1.30, "low",  "literature"),
    ("Barley",         "Ferulic acid",   277.00, "high", "literature"),
    ("Rye",            "Ferulic acid",   340.00, "high", "literature"),
    ("Rosemary",       "Caffeic acid",    70.00, "high", "literature"),
    ("Oregano",        "Caffeic acid",    63.00, "high", "literature"),
    ("Thyme",          "Caffeic acid",    24.00, "medium","literature"),
    ("Basil",          "Caffeic acid",    28.00, "medium","literature"),
    ("Peppermint",     "Caffeic acid",   165.00, "high", "literature"),
    ("Spinach",        "Caffeic acid",    10.00, "medium","literature"),
    ("Artichoke",      "Rosmarinic acid", 15.00, "medium","literature"),
    ("Peppermint",     "Rosmarinic acid",330.00, "high", "literature"),
    ("Lemongrass",     "Caffeic acid",    11.00, "medium","literature"),
    # Terpenoids / spices
    ("Orange",         "Limonene",      160.00, "high",  "literature"),
    ("Lemon",          "Limonene",      420.00, "high",  "literature"),
    ("Grapefruit",     "Limonene",      180.00, "high",  "literature"),
    ("Black pepper",   "Limonene",       56.00, "medium","literature"),
    ("Dill",           "Limonene",      230.00, "high",  "literature"),
    ("Fennel",         "Limonene",       52.00, "medium","literature"),
    ("Thyme",          "Thymol",        900.00, "high",  "literature"),
    ("Thyme",          "Carvacrol",      80.00, "high",  "literature"),
    ("Oregano",        "Carvacrol",    2110.00, "high",  "literature"),
    ("Oregano",        "Thymol",        890.00, "high",  "literature"),
    ("Rosemary",       "Ursolic acid",  2700.00, "high", "literature"),
    ("Basil",          "Ursolic acid",   560.00, "high", "literature"),
    ("Apple",          "Ursolic acid",   470.00, "high", "literature"),
    ("Cranberry",      "Ursolic acid",   310.00, "high", "literature"),
    ("Elderberry",     "Ursolic acid",   280.00, "high", "literature"),
    # Organosulfur expanded
    ("Garlic",         "Diallyl disulfide", 60.00, "high","literature"),
    ("Shallot",        "Allicin",          20.00, "medium","literature"),
    ("Yellow onion",   "Allicin",           5.00, "medium","literature"),
    ("Leek",           "Diallyl disulfide", 2.00, "low", "literature"),
    ("Broccoli",       "Indole-3-carbinol",37.00, "high", "literature"),
    ("Broccoli","Diindolylmethane",         2.50, "medium","literature"),
    ("Cabbage",        "Indole-3-carbinol",25.00, "medium","literature"),
    ("Kale",           "Indole-3-carbinol",30.00, "high", "literature"),
    ("Brussels sprouts","Indole-3-carbinol",50.00,"high", "literature"),
    ("Cauliflower",    "Indole-3-carbinol",20.00, "medium","literature"),
    ("Watercress",     "Sulforaphane",     15.00, "high", "literature"),
    ("Wasabi",         "Sulforaphane",     30.00, "high", "literature"),
    # Olive & olive oil phenolics
    ("Olive oil (extra virgin)","Oleuropein",    290.00, "high","literature"),
    ("Olive oil (extra virgin)","Tyrosol",         44.00, "high","literature"),
    ("Olive oil (extra virgin)","Hydroxytyrosol",  20.00, "medium","literature"),
    # Fatty acids expanded
    ("Flaxseed",       "Oleic acid",    2280.00, "high",  "USDA"),
    ("Olive oil (extra virgin)","Oleic acid",73000.00,"high","USDA"),
    ("Almond",         "Oleic acid",   33600.00, "high",  "USDA"),
    ("Avocado",        "Oleic acid",    9800.00, "high",  "USDA"),
    ("Canola oil",     "Oleic acid",   63600.00, "high",  "USDA"),
    ("Walnut",         "Oleic acid",   14300.00, "high",  "USDA"),
    ("Chia seed",      "Oleic acid",    7000.00, "high",  "USDA"),
    ("Hemp seed",      "Oleic acid",    5100.00, "high",  "literature"),
    ("Salmon",         "Oleic acid",    3890.00, "high",  "USDA"),
    ("Salmon",         "Alpha-linolenic acid",  411.00, "medium","USDA"),
    ("Walnut",         "Alpha-linolenic acid", 9080.00, "high",  "USDA"),
    ("Flaxseed",       "Conjugated linoleic acid", 0.50, "low", "literature"),
    ("Tuna",           "Eicosapentaenoic acid",  110.00, "medium","USDA"),
    ("Anchovy",        "Docosahexaenoic acid",   911.00, "high", "USDA"),
    ("Anchovy",        "Eicosapentaenoic acid",  763.00, "high", "USDA"),
    ("Sardine",        "Alpha-linolenic acid",   509.00, "medium","literature"),
    ("Chlorella",      "Alpha-linolenic acid",   208.00, "medium","literature"),
    ("Spirulina",      "Gamma-linolenic acid",   140.00, "high", "literature"),
    ("Hemp seed",      "Gamma-linolenic acid",   770.00, "high", "literature"),
    ("Walnut",         "Conjugated linoleic acid", 0.30, "low", "literature"),
    # Carotenoids expanded
    ("Salmon",         "Astaxanthin",     2.40, "medium","literature"),
    ("Shrimp",         "Astaxanthin",     1.20, "medium","literature"),
    ("Krill",          "Astaxanthin",    29.50, "high",  "literature"),
    ("Carrot",         "Alpha-carotene",3477.00, "high", "USDA"),
    ("Pumpkin",        "Alpha-carotene",4016.00, "high", "literature"),
    ("Cantaloupe",     "Alpha-carotene",  16.00, "low",  "USDA"),
    ("Apricot",        "Beta-cryptoxanthin", 104.00, "high","USDA"),
    ("Orange",         "Beta-cryptoxanthin", 116.00, "high","USDA"),
    ("Papaya",         "Beta-cryptoxanthin", 761.00, "high","USDA"),
    ("Mango",          "Beta-cryptoxanthin",  11.00, "low", "USDA"),
    ("Seaweed (wakame)","Fucoxanthin",    1300.00, "high","literature"),
    ("Seaweed (nori)", "Fucoxanthin",      500.00, "high","literature"),
    ("Spinach",        "Lutein",          12.20, "high", "USDA"),
    ("Kale",           "Zeaxanthin",       2.32, "medium","USDA"),
    ("Corn",           "Lutein",           0.97, "medium","literature"),
    ("Corn",           "Zeaxanthin",       0.58, "low",  "literature"),
    # Vitamins expanded
    ("Broccoli",       "Ascorbic acid",   89.20, "high", "USDA"),
    ("Kale",           "Ascorbic acid",   93.40, "high", "USDA"),
    ("Bell pepper",    "Ascorbic acid",  127.70, "high", "USDA"),
    ("Red bell pepper","Ascorbic acid",  190.00, "high", "USDA"),
    ("Parsley",        "Ascorbic acid",  133.00, "high", "USDA"),
    ("Spinach",        "Ascorbic acid",   28.10, "medium","USDA"),
    ("Strawberry",     "Ascorbic acid",   58.80, "high", "USDA"),
    ("Orange",         "Ascorbic acid",   53.20, "high", "USDA"),
    ("Kiwi",           "Ascorbic acid",   92.70, "high", "USDA"),
    ("Lemon",          "Ascorbic acid",   53.00, "high", "USDA"),
    ("Guava",          "Ascorbic acid",  228.30, "high", "USDA"),
    ("Papaya",         "Ascorbic acid",   61.80, "high", "USDA"),
    ("Pineapple",      "Ascorbic acid",   47.80, "high", "USDA"),
    ("Mango",          "Ascorbic acid",   36.40, "medium","USDA"),
    ("Tomato",         "Ascorbic acid",   13.70, "medium","USDA"),
    ("Almond",         "Alpha-tocopherol",25.63, "high", "USDA"),
    ("Sunflower seed", "Alpha-tocopherol",35.17, "high", "USDA"),
    ("Walnut",         "Alpha-tocopherol", 0.70, "low",  "USDA"),
    ("Hazelnut",       "Alpha-tocopherol",15.03, "high", "USDA"),
    ("Spinach",        "Alpha-tocopherol", 2.03, "medium","USDA"),
    ("Broccoli",       "Alpha-tocopherol", 0.78, "low",  "USDA"),
    ("Avocado",        "Alpha-tocopherol", 2.07, "medium","USDA"),
    ("Salmon",         "Vitamin D3",       5.60, "high", "USDA"),
    ("Herring",        "Vitamin D3",       5.00, "high", "literature"),
    ("Sardine",        "Vitamin D3",       4.80, "high", "USDA"),
    ("Tuna",           "Vitamin D3",       3.68, "medium","USDA"),
    ("Rainbow trout",  "Vitamin D3",       7.37, "high", "USDA"),
    ("Mushroom",       "Vitamin D3",      19.00, "high", "literature"),
    # CoQ10 expanded
    ("Atlantic mackerel","Coenzyme Q10",  6.75, "medium","literature"),
    ("Sardine",        "Coenzyme Q10",   6.40, "medium","literature"),
    ("Beef liver",     "Coenzyme Q10",   3.90, "medium","literature"),
    ("Broccoli",       "Coenzyme Q10",   0.86, "low",   "literature"),
    ("Cauliflower",    "Coenzyme Q10",   0.60, "low",   "literature"),
    ("Spinach",        "Coenzyme Q10",   1.00, "low",   "literature"),
    ("Peanut",         "Coenzyme Q10",   2.67, "medium","literature"),
    ("Pistachio",      "Coenzyme Q10",   2.00, "medium","literature"),
    # Melatonin food sources
    ("Tart cherry",    "Melatonin",      0.0135, "low",  "literature"),
    ("Walnut",         "Melatonin",      0.0039, "low",  "literature"),
    ("Almond",         "Melatonin",      0.0005, "low",  "literature"),
    ("Pineapple",      "Melatonin",      0.0017, "low",  "literature"),
    ("Tomato",         "Melatonin",      0.0005, "low",  "literature"),
    ("Oats",           "Melatonin",      0.0094, "low",  "literature"),
    ("Barley",         "Melatonin",      0.0083, "low",  "literature"),
    ("Rice",           "Melatonin",      0.0038, "low",  "literature"),
    # Spermidine expanded
    ("Natto",          "Spermidine",     241.00, "high", "literature"),
    ("Soybean",        "Spermidine",     207.00, "high", "literature"),
    ("Peas",           "Spermidine",      67.00, "high", "literature"),
    ("Broccoli",       "Spermidine",      31.00, "high", "literature"),
    ("Cauliflower",    "Spermidine",      29.00, "high", "literature"),
    ("Mushroom",       "Spermidine",      89.00, "high", "literature"),
    ("Corn",           "Spermidine",      24.00, "medium","literature"),
    ("Chickpea",       "Spermidine",     289.00, "high", "literature"),
    ("Lentil",         "Spermidine",     289.00, "high", "literature"),
    # Ergothioneine
    ("Oyster mushroom","Ergothioneine",  1300.00, "high","literature"),
    ("Shiitake",       "Ergothioneine",  2040.00, "high","literature"),
    ("Porcini",        "Ergothioneine",   4380.00,"high","literature"),
    ("King oyster mushroom","Ergothioneine",4850.00,"high","literature"),
    ("Black bean",     "Ergothioneine",     0.18, "low", "literature"),
    # Berberine sources
    ("Barberry",       "Berberine",      8000.00, "high","literature"),
    ("Golden seal",    "Berberine",      3000.00, "high","literature"),
    ("Oregon grape",   "Berberine",      2100.00, "high","literature"),
    # Silymarin
    ("Milk thistle",   "Silymarin",     15000.00, "high","literature"),
    # Honokiol / magnolol
    ("Magnolia bark",  "Honokiol",       1500.00, "high","literature"),
    # Andrographolide
    ("Andrographis",   "Andrographolide",1000.00, "high","literature"),
    # Piperine expanded
    ("White pepper",   "Piperine",       3000.00, "high","literature"),
    ("Black pepper",   "Piperine",       5000.00, "high","literature"),
    # Capsaicin extended
    ("Paprika",        "Capsaicin",       280.00, "high","literature"),
    ("Cayenne pepper", "Capsaicin",      2500.00, "high","literature"),
    # Gingerol extended
    ("Turmeric",       "Gingerol",         50.00, "medium","literature"),
    ("Ginger",         "Gingerol",        825.00, "high", "literature"),
    # Curcuminoid companions
    ("Turmeric",       "Demethoxycurcumin",260.00,"high","literature"),
    # Lignans
    ("Flaxseed",       "Secoisolariciresinol",3709.00,"high","literature"),
    ("Sesame seed",    "Matairesinol",    148.00, "high","literature"),
    ("Sesame seed",    "Secoisolariciresinol",29.00,"high","literature"),
    ("Rye",            "Secoisolariciresinol",11.80,"medium","literature"),
    ("Barley",         "Secoisolariciresinol", 6.60,"medium","literature"),
    ("Oats",           "Secoisolariciresinol", 3.20,"medium","literature"),
    ("Broccoli",       "Secoisolariciresinol", 0.55,"low",  "literature"),
    ("Kale",           "Secoisolariciresinol", 0.68,"low",  "literature"),
    # Alpha-lipoic acid food sources
    ("Spinach",        "Alpha-lipoic acid",   3.15, "medium","literature"),
    ("Broccoli",       "Alpha-lipoic acid",   2.50, "medium","literature"),
    ("Tomato",         "Alpha-lipoic acid",   0.72, "low",  "literature"),
    ("Peas",           "Alpha-lipoic acid",   0.68, "low",  "literature"),
    ("Brussels sprouts","Alpha-lipoic acid",  1.03, "low",  "literature"),
    # Urolithin A precursors (ellagitannin-rich)
    ("Pomegranate",    "Urolithin A",         1.20, "low",  "literature"),
    ("Raspberry",      "Urolithin A",         0.80, "low",  "literature"),
    ("Strawberry",     "Urolithin A",         0.40, "low",  "literature"),
    ("Walnut",         "Urolithin A",         0.30, "low",  "literature"),
    # Crocin / Crocetin (saffron)
    ("Saffron",        "Crocetin",         1200.00, "high","literature"),
    # Theobromine expanded
    ("Cacao nibs",     "Theobromine",      1764.00, "high","literature"),
    ("Green tea",      "Theobromine",         2.40, "low", "literature"),
    # Rutin
    ("Buckwheat",      "Rutin",            1000.00, "high","literature"),
    ("Capers",         "Rutin",              89.00, "high","literature"),
    ("Asparagus",      "Rutin",               3.00, "medium","literature"),
    ("Tomato",         "Rutin",               0.60, "low", "literature"),
    ("Apple",          "Rutin",               0.50, "low", "literature"),
    ("Elderberry",     "Rutin",              17.00, "high","literature"),
    ("Green tea",      "Rutin",               4.38, "medium","literature"),
    ("Black tea",      "Rutin",               3.51, "medium","literature"),
    # Fisetin
    ("Strawberry",     "Fisetin",            16.00, "high","literature"),
    ("Apple",          "Fisetin",             2.60, "medium","literature"),
    ("Persimmon",      "Fisetin",             0.80, "low", "literature"),
    ("Kiwi",           "Fisetin",             0.46, "low", "literature"),
    ("Peach",          "Fisetin",             0.63, "low", "literature"),
    # Naringenin sources
    ("Tomato",         "Naringenin",          0.61, "low", "literature"),
    ("Grapefruit",     "Hesperidin",         61.60, "high","literature"),
    ("Orange",         "Hesperidin",         47.80, "high","literature"),
    ("Lemon",          "Hesperidin",         35.00, "high","literature"),
    ("Grapefruit",     "Eriodictyol",         4.20, "medium","literature"),
    # Myricetin
    ("Cranberry",      "Myricetin",          13.00, "high","literature"),
    ("Gooseberry",     "Myricetin",          29.00, "high","literature"),
    ("Black currant",  "Myricetin",          18.00, "high","literature"),
    ("Red onion",      "Myricetin",           7.00, "medium","literature"),
    ("Spinach",        "Myricetin",           0.07, "low", "literature"),
    # Vanillin
    ("Vanilla",        "Vanillin",         2800.00, "high","literature"),
    ("Dark chocolate", "Vanillin",           30.00, "medium","literature"),
    # Sinapic acid
    ("Broccoli",       "Sinapic acid",       22.00, "medium","literature"),
    ("Mustard",        "Sinapic acid",      176.00, "high","literature"),
    ("Canola",         "Sinapic acid",      140.00, "high","literature"),
    ("Kale",           "Sinapic acid",       13.00, "medium","literature"),
    # p-Coumaric acid
    ("Peanut",         "p-Coumaric acid",    22.00, "medium","literature"),
    ("Tomato",         "p-Coumaric acid",     1.50, "low", "literature"),
    ("Carrot",         "p-Coumaric acid",     0.60, "low", "literature"),
    ("Oats",           "p-Coumaric acid",    15.00, "medium","literature"),
    # Protocatechuic acid
    ("Blueberry",      "Protocatechuic acid",  8.00, "medium","literature"),
    ("Raspberry",      "Protocatechuic acid",  5.10, "medium","literature"),
    ("Onion",          "Protocatechuic acid",  3.00, "medium","literature"),
    ("Pomegranate",    "Protocatechuic acid", 22.00, "high","literature"),
    # Syringic acid
    ("Red wine",       "Syringic acid",       5.00, "medium","literature"),
    ("Blueberry",      "Syringic acid",       1.20, "low", "literature"),
    ("Pomegranate",    "Syringic acid",      11.00, "medium","literature"),
    # Pterostilbene more sources
    ("Grape",          "Pterostilbene",       0.04, "low", "literature"),
    ("Cranberry",      "Pterostilbene",       0.03, "low", "literature"),
    # Oleanolic acid
    ("Olive oil (extra virgin)","Oleanolic acid",2700.00,"high","literature"),
    ("Rosemary",       "Oleanolic acid",    2100.00, "high","literature"),
    ("Thyme",          "Oleanolic acid",    1800.00, "high","literature"),
    ("Sage",           "Oleanolic acid",    1200.00, "high","literature"),
    # Betulinic acid
    ("Rosemary",       "Betulinic acid",     180.00, "high","literature"),
    ("Birch tea",      "Betulinic acid",     120.00, "high","literature"),
    # Baicalein (from Scutellaria)
    ("Baicalein root", "Baicalein",         5000.00, "high","literature"),
    # Taxifolin
    ("Siberian larch", "Taxifolin",        80000.00, "high","literature"),
    ("Red onion",      "Taxifolin",            4.00, "medium","literature"),
    ("Apple",          "Taxifolin",            0.80, "low", "literature"),
    # African / diaspora foods
    ("Moringa",        "Ascorbic acid",      220.00, "high","literature"),
    ("Moringa",        "Alpha-tocopherol",    8.75, "medium","literature"),
    ("Moringa",        "Zeaxanthin",         40.00, "high","literature"),
    ("Moringa",        "Lutein",             57.00, "high","literature"),
    ("Baobab",         "Quercetin",          14.00, "high","literature"),
    ("Baobab",         "Rutin",              18.00, "high","literature"),
    ("Bitter leaf",    "Luteolin",            8.40, "medium","literature"),
    ("Bitter leaf",    "Apigenin",            5.20, "medium","literature"),
    ("Bitter leaf",    "Quercetin",           6.20, "medium","literature"),
    ("Bitter leaf",    "Ascorbic acid",      117.00, "high","literature"),
    ("Bitter leaf",    "Beta-carotene",     3150.00, "high","literature"),
    ("Fluted pumpkin", "Beta-carotene",     4700.00, "high","literature"),
    ("Fluted pumpkin", "Ascorbic acid",       46.00, "high","literature"),
    ("Fluted pumpkin", "Alpha-tocopherol",    2.00, "medium","literature"),
    ("Cassava leaf",   "Beta-carotene",      828.00, "high","literature"),
    ("Cassava leaf",   "Ascorbic acid",      153.00, "high","literature"),
    ("Jute leaf",      "Beta-carotene",     3130.00, "high","literature"),
    ("Jute leaf",      "Luteolin",           18.00, "high","literature"),
    ("Jute leaf",      "Quercetin",          12.00, "high","literature"),
    ("Jute leaf",      "Ascorbic acid",       53.00, "high","literature"),
    ("Scent leaf",     "Luteolin",           28.00, "high","literature"),
    ("Scent leaf",     "Rosmarinic acid",    68.00, "high","literature"),
    ("Scent leaf",     "Caffeic acid",       42.00, "high","literature"),
    ("Prekese",        "Quercetin",          16.00, "high","literature"),
    ("Prekese",        "Kaempferol",          8.00, "medium","literature"),
    ("Tiger nut",      "Oleic acid",        1850.00,"high","literature"),
    ("Tiger nut",      "Alpha-tocopherol",    1.32, "low", "literature"),
    ("Red palm oil",   "Beta-carotene",    15000.00,"high","literature"),
    ("Red palm oil",   "Alpha-carotene",    4900.00,"high","literature"),
    ("Red palm oil",   "Alpha-tocopherol",  1560.00,"high","literature"),
    ("Bitter kola",    "Caffeine",           27.00, "medium","literature"),
    ("Bitter kola",    "Quercetin",           4.00, "medium","literature"),
    ("Kola nut",       "Caffeine",          370.00, "high","literature"),
    ("Kola nut",       "Theobromine",       130.00, "high","literature"),
    ("Uziza leaf",     "Piperine",          400.00, "high","literature"),
    ("Uziza leaf",     "Quercetin",          11.00, "medium","literature"),
    ("African walnut", "Ellagic acid",       80.00, "high","literature"),
    ("African walnut", "Gallic acid",        55.00, "high","literature"),
    ("Agbalumo",       "Ascorbic acid",     100.00, "high","literature"),
    ("Agbalumo",       "Ellagic acid",       42.00, "high","literature"),
    ("Pearl millet",   "Ferulic acid",      340.00, "high","literature"),
    ("Sorghum",        "Ferulic acid",      360.00, "high","literature"),
    ("Fonio",          "Ferulic acid",      120.00, "high","literature"),
    ("Shea butter",    "Alpha-tocopherol",  8300.00,"high","literature"),
    ("Shea butter",    "Oleanolic acid",    4100.00,"high","literature"),
    # Whole grains expanded
    ("Barley",         "Quercetin",           0.30, "low", "literature"),
    ("Oats",           "Caffeic acid",       13.00, "medium","literature"),
    ("Quinoa",         "Quercetin",           1.00, "low", "literature"),
    ("Quinoa",         "Kaempferol",          0.40, "low", "literature"),
    ("Buckwheat",      "Quercetin",          36.00, "high","literature"),
    ("Buckwheat",      "Vitexin",           126.00, "high","literature"),
    ("Amaranth",       "Rutin",               7.80, "medium","literature"),
    ("Rye",            "Quercetin",           0.28, "low", "literature"),
    # Legumes expanded
    ("Black bean",     "Quercetin",          11.00, "medium","literature"),
    ("Black bean",     "Kaempferol",          3.10, "medium","literature"),
    ("Kidney bean",    "Quercetin",           5.50, "medium","literature"),
    ("Chickpea",       "Quercetin",           0.71, "low", "literature"),
    ("Peanut",         "Resveratrol",         0.05, "low", "literature"),
    ("Peanut",         "Coenzyme Q10",        2.67, "medium","literature"),
    # Nuts expanded
    ("Pecan",          "Ellagic acid",      330.00, "high","literature"),
    ("Pecan",          "Resveratrol",         0.06, "low", "literature"),
    ("Almond",         "Quercetin",           0.53, "low", "literature"),
    ("Hazelnut",       "Quercetin",           0.78, "low", "literature"),
    ("Hazelnut",       "Caffeic acid",        0.70, "low", "literature"),
    ("Pistachio",      "Resveratrol",         0.33, "low", "literature"),
    ("Pistachio",      "Quercetin",           1.09, "low", "literature"),
    ("Brazil nut",     "Ellagic acid",       23.00, "medium","literature"),
    # Mushrooms expanded
    ("Seaweed (nori)", "Astaxanthin",       0.40, "low",   "literature"),
    ("Seaweed (wakame)","Astaxanthin",      0.60, "low",   "literature"),
    ("Spirulina",      "Chlorogenic acid",  0.30, "low",   "literature"),
    ("Chlorella",      "Lutein",            7.50, "medium","literature"),
    ("Chlorella",      "Beta-carotene",    14.00, "high",  "literature"),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

async def load_maps(db: Any) -> tuple[
    dict[str, str],  # gene_symbol → uuid
    dict[str, str],  # pathway_id → uuid
    dict[str, str],  # compound_name → uuid
    dict[str, str],  # food name → uuid
]:
    gene_resp    = await db.table("genes")   .select("id, gene_symbol").execute()
    pathway_resp = await db.table("pathways").select("id, pathway_id").execute()
    cmpd_resp    = await db.table("compounds").select("id, compound_name").execute()
    food_resp    = await db.table("foods")   .select("id, name").execute()

    gene_map    = {r["gene_symbol"]:   r["id"] for r in (gene_resp.data or [])}
    pathway_map = {r["pathway_id"]:    r["id"] for r in (pathway_resp.data or [])}
    cmpd_map    = {r["compound_name"]: r["id"] for r in (cmpd_resp.data or [])}
    food_map    = {r["name"]:          r["id"] for r in (food_resp.data or [])}

    logger.info(
        "Loaded from DB: %d genes, %d pathways, %d compounds, %d foods",
        len(gene_map), len(pathway_map), len(cmpd_map), len(food_map),
    )
    return gene_map, pathway_map, cmpd_map, food_map


async def get_existing_pairs(db: Any, table: str, col_a: str, col_b: str) -> set[tuple[str, str]]:
    resp = await db.table(table).select(f"{col_a}, {col_b}").execute()
    return {(r[col_a], r[col_b]) for r in (resp.data or [])}


# ── Gene-pathway associations ─────────────────────────────────────────────────

async def fix_gene_pathway_associations(db: Any, dry_run: bool) -> int:
    gene_map, pathway_map, _, _ = await load_maps(db)

    existing = await get_existing_pairs(
        db, "gene_pathway_associations", "gene_id", "pathway_id"
    )
    logger.info("Existing gene_pathway_associations: %d", len(existing))

    records: list[dict[str, Any]] = []
    skipped_no_gene = 0
    skipped_no_pathway = 0
    skipped_dup = 0

    for gene_sym, pathway_ids in GENE_PATHWAY_MAP.items():
        gene_uuid = gene_map.get(gene_sym)
        if not gene_uuid:
            skipped_no_gene += 1
            continue
        for kegg_pid in pathway_ids:
            pw_uuid = pathway_map.get(kegg_pid)
            if not pw_uuid:
                skipped_no_pathway += 1
                continue
            if (gene_uuid, pw_uuid) in existing:
                skipped_dup += 1
                continue
            records.append({
                "gene_id":          gene_uuid,
                "pathway_id":       pw_uuid,
                "confidence_score": 0.80,
            })
            existing.add((gene_uuid, pw_uuid))   # prevent within-batch dups

    logger.info(
        "gene_pathway_associations: %d to insert, "
        "skipped %d (no gene), %d (no pathway), %d (already exists)",
        len(records), skipped_no_gene, skipped_no_pathway, skipped_dup,
    )

    if not dry_run and records:
        inserted = await batch_insert(db, "gene_pathway_associations", records, logger=logger)
        logger.info("Inserted %d gene_pathway_association rows.", inserted)
        return inserted
    elif dry_run:
        logger.info("[DRY RUN] Would insert %d gene_pathway_association rows.", len(records))
    return len(records)


# ── Compound-gene interactions ────────────────────────────────────────────────

async def fix_compound_gene_interactions(db: Any, dry_run: bool) -> int:
    from populate_gene_compound_interactions import CURATED_INTERACTIONS  # noqa: F401

    gene_map, _, cmpd_map, _ = await load_maps(db)

    existing = await get_existing_pairs(
        db, "compound_gene_interactions", "compound_id", "gene_id"
    )
    logger.info("Existing compound_gene_interactions: %d", len(existing))

    all_interactions = [
        (c, g, itype, score, evtype, "literature")
        for (c, g, itype, score, evtype, _src) in CURATED_INTERACTIONS
    ] + [
        (c, g, itype, score, evtype, "literature")
        for (c, g, itype, score, evtype) in EXTENDED_INTERACTIONS
    ]

    records: list[dict[str, Any]] = []
    skipped = 0

    for (cname, gsym, itype, score, evtype, src) in all_interactions:
        cmpd_uuid = cmpd_map.get(cname)
        gene_uuid = gene_map.get(gsym)
        if not cmpd_uuid or not gene_uuid:
            skipped += 1
            continue
        key = (cmpd_uuid, gene_uuid)
        if key in existing:
            continue
        records.append({
            "compound_id":       cmpd_uuid,
            "gene_id":           gene_uuid,
            "interaction_type":  itype,
            "bioactivity_score": score,
            "evidence_type":     evtype,
            "source":            src,
        })
        existing.add(key)

    logger.info(
        "compound_gene_interactions: %d to insert, %d skipped (missing gene/compound)",
        len(records), skipped,
    )

    if not dry_run and records:
        inserted = await batch_insert(db, "compound_gene_interactions", records, logger=logger)
        logger.info("Inserted %d compound_gene_interaction rows.", inserted)
        return inserted
    elif dry_run:
        logger.info("[DRY RUN] Would insert %d compound_gene_interaction rows.", len(records))
    return len(records)


# ── Food-compound associations ────────────────────────────────────────────────

async def fix_food_compound_associations(db: Any, dry_run: bool) -> int:
    from populate_food_sources import FOOD_COMPOUND_ASSOCIATIONS  # noqa: F401

    _, _, cmpd_map, food_map = await load_maps(db)

    existing = await get_existing_pairs(
        db, "food_compound_associations", "food_id", "compound_id"
    )
    logger.info("Existing food_compound_associations: %d", len(existing))

    all_fca = list(FOOD_COMPOUND_ASSOCIATIONS) + list(EXTENDED_FCA)
    records: list[dict[str, Any]] = []
    skipped = 0

    for (fname, cname, conc, category, src) in all_fca:
        food_uuid = food_map.get(fname)
        cmpd_uuid = cmpd_map.get(cname)
        if not food_uuid or not cmpd_uuid:
            skipped += 1
            continue
        key = (food_uuid, cmpd_uuid)
        if key in existing:
            continue
        records.append({
            "food_id":                   food_uuid,
            "compound_id":               cmpd_uuid,
            "concentration_mg_per_100g": conc,
            "concentration_category":    category,
            "source":                    src,
        })
        existing.add(key)

    logger.info(
        "food_compound_associations: %d to insert, %d skipped (missing food/compound)",
        len(records), skipped,
    )

    if not dry_run and records:
        inserted = await batch_insert(db, "food_compound_associations", records, logger=logger)
        logger.info("Inserted %d food_compound_association rows.", inserted)
        return inserted
    elif dry_run:
        logger.info("[DRY RUN] Would insert %d food_compound_association rows.", len(records))
    return len(records)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(dry_run: bool = False, table: str = "all") -> None:
    from app.database import get_supabase

    db = await get_supabase()
    run_gpa = table in ("all", "gpa")
    run_cgi = table in ("all", "cgi")
    run_fca = table in ("all", "fca")

    totals: dict[str, int] = {}

    if run_gpa:
        logger.info("=" * 60)
        logger.info("FIXING gene_pathway_associations")
        totals["gpa"] = await fix_gene_pathway_associations(db, dry_run)

    if run_cgi:
        logger.info("=" * 60)
        logger.info("FIXING compound_gene_interactions")
        totals["cgi"] = await fix_compound_gene_interactions(db, dry_run)

    if run_fca:
        logger.info("=" * 60)
        logger.info("FIXING food_compound_associations")
        totals["fca"] = await fix_food_compound_associations(db, dry_run)

    logger.info("=" * 60)
    logger.info("DONE%s", " [DRY RUN]" if dry_run else "")
    for key, n in totals.items():
        action = "Would insert" if dry_run else "Inserted"
        logger.info("  %s: %s %d rows", key, action, n)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fix junction tables using existing main-table rows."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without inserting")
    parser.add_argument("--table",   default="all",
                        choices=["all", "gpa", "cgi", "fca"],
                        help="Which table(s) to fix (default: all)")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run, table=args.table))
