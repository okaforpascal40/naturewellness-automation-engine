"""Expand phytochemical_sources with common fruits, vegetables, African plants,
herbs, spices and teas, so plant search covers what users actually type.

How it works:
  1. Read every distinct chemical_name in ctd_phytochemical_gene_interactions.
  2. Invert the hand-curated PLANTS mapping below (plant → phytochemicals) into
     phytochemical → plants, resolving each compound to its exact CTD spelling.
     Compounds CTD has no gene interactions for are skipped and reported: a
     plant search could not grade them anyway.
  3. Upsert into phytochemical_sources. Existing rows keep their spelling,
     primary_sources and chemical_class; the new plants are unioned into their
     fruit_vegetables, so nothing already curated is lost.

Local names are listed as separate plants (Zobo, Roselle and Hibiscus share one
compound list) so a search for any of them resolves.

Usage:
    python scripts/expand_plant_sources.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from supabase import Client, create_client

ROOT = Path(__file__).resolve().parent.parent
SOURCES_TABLE = "phytochemical_sources"
CTD_TABLE = "ctd_phytochemical_gene_interactions"
PAGE_SIZE = 1000
BATCH_SIZE = 100
PRIMARY_SOURCES_PER_ROW = 3

# ── Shared compound lists (one plant, several names) ─────────────────────────
_PAPAYA = ["Papain", "Lycopene", "beta Carotene", "beta-Cryptoxanthin", "Benzyl isothiocyanate",
           "Quercetin", "Kaempferol", "Chlorogenic acid", "Caffeic acid", "Ferulic acid"]
_CHILI = ["Capsaicin", "Dihydrocapsaicin", "Capsanthin", "Luteolin", "Quercetin",
          "beta Carotene", "Lutein", "Ascorbic acid"]
_BELL_PEPPER = ["Capsanthin", "beta Carotene", "beta-Cryptoxanthin", "Lutein", "Zeaxanthin",
                "Luteolin", "Quercetin", "Ascorbic acid"]
_PEA = ["Coumestrol", "Catechin", "Epicatechin", "Lutein", "Zeaxanthin", "Kaempferol",
        "Quercetin", "Saponins"]
_GREEN_BEAN = ["Quercetin", "Kaempferol", "Catechin", "Epicatechin", "Myricetin", "Lutein",
               "beta Carotene"]
_FLUTED_PUMPKIN = ["beta Carotene", "Lutein", "Quercetin", "Kaempferol", "Rutin",
                   "Gallic acid", "Ascorbic acid", "Saponins"]
_SCENT_LEAF = ["Eugenol", "Thymol", "Linalool", "Methyleugenol", "Rosmarinic acid",
               "gamma-Terpinene", "p-Cymene", "beta-Caryophyllene", "Oleanolic acid",
               "Ursolic acid", "Apigenin"]
_UZIZA = ["Piperine", "Dillapiole", "Myristicin", "Elemicin", "Safrole", "Linalool",
          "beta-Caryophyllene", "Limonene"]
_UTAZI = ["beta-Sitosterol", "Lupeol", "Saponins", "Quercetin", "Kaempferol", "Rutin",
          "Linalool"]
_MORINGA = ["Quercetin", "Kaempferol", "Chlorogenic acid", "Isothiocyanates", "Moringin",
            "Niazimicin", "Isoquercitrin", "Astragalin", "Rutin", "beta Carotene",
            "Ascorbic acid", "beta-Sitosterol", "Zeatin"]
_NEEM = ["Azadirachtin", "Nimbolide", "Nimbin", "Gedunin", "Salannin", "Azadiradione",
         "Quercetin", "beta-Sitosterol"]
_STAR_APPLE = ["Ascorbic acid", "Myricetin", "Quercetin", "Catechin", "Epicatechin",
               "Gallic acid"]
_HIBISCUS = ["Anthocyanins", "Delphinidin", "Cyanidin", "Quercetin", "Chlorogenic acid",
             "Protocatechuic acid", "Hibiscus acid", "Gossypetin", "Myricetin", "Gallic acid",
             "Ascorbic acid"]
_SHEA = ["Lupeol", "alpha-Amyrin", "beta-Amyrin", "Oleic acid", "Stearic acid",
         "alpha-Tocopherol", "Catechin", "Epigallocatechin", "Gallic acid"]
_NEGRO_PEPPER = ["Kaurenoic acid", "beta-Pinene", "alpha-Pinene", "Sabinene", "Eucalyptol",
                 "Linalool", "Terpinen-4-ol", "Limonene"]
_EHURU = ["alpha-Phellandrene", "p-Cymene", "Limonene", "alpha-Pinene", "Linalool",
          "Myristicin"]
_MONKEY_KOLA = ["beta Carotene", "Ascorbic acid", "Catechin", "Quercetin"]
_BUSH_MANGO = ["Ellagic acid", "Gallic acid", "Kaempferol", "Quercetin", "Myristic acid",
               "Lauric acid", "beta-Sitosterol"]
_GINGER = ["Gingerol", "Shogaol", "Zingerone", "Paradol", "Zingiberene", "beta-Bisabolene"]
_PEPPERMINT = ["Menthol", "Menthone", "Rosmarinic acid", "Eriocitrin", "Luteolin",
               "Hesperidin", "Limonene", "Eucalyptol", "Pulegone"]

# ── Plant → phytochemicals ───────────────────────────────────────────────────
# Common compound names; each resolves to its CTD spelling via CTD_ALIASES or a
# case-insensitive match. Lists favour well-characterised constituents.
PLANTS: dict[str, list[str]] = {
    # Fruits
    "Banana": ["Dopamine", "Serotonin", "Quercetin", "Catechin", "Epicatechin", "Gallic acid",
               "Ferulic acid", "Rutin", "Myricetin", "Lutein", "beta Carotene"],
    "Mango": ["Mangiferin", "Quercetin", "Kaempferol", "Gallic acid", "Ellagic acid", "Catechin",
              "Lupeol", "beta Carotene", "Lutein", "Chlorogenic acid"],
    "Orange": ["Hesperidin", "Hesperetin", "Naringenin", "Nobiletin", "Tangeretin", "Limonene",
               "beta-Cryptoxanthin", "beta Carotene", "Lutein", "Ferulic acid", "Caffeic acid",
               "Ascorbic acid"],
    "Lemon": ["Limonene", "Citral", "Hesperidin", "Eriocitrin", "Diosmin", "Naringenin", "Rutin",
              "Quercetin", "Ascorbic acid"],
    "Lime": ["Limonene", "Citral", "Hesperidin", "Naringenin", "Rutin", "Quercetin", "Linalool",
             "Ascorbic acid"],
    "Pineapple": ["Bromelains", "Ferulic acid", "Caffeic acid", "Chlorogenic acid", "beta Carotene",
                  "Ascorbic acid"],
    "Papaya": _PAPAYA,
    "Pawpaw": _PAPAYA,
    "Watermelon": ["Lycopene", "Citrulline", "beta Carotene", "Lutein", "Cucurbitacin E"],
    "Apple": ["Quercetin", "Phloretin", "Phlorizin", "Catechin", "Epicatechin", "Chlorogenic acid",
              "Procyanidin", "Ursolic acid", "Rutin", "Hyperoside", "Quercitrin", "Isoquercitrin"],
    "Grape": ["Resveratrol", "Quercetin", "Catechin", "Epicatechin", "Anthocyanins",
              "Proanthocyanidins", "Myricetin", "Kaempferol", "Gallic acid", "Ellagic acid",
              "Pterostilbene", "Delphinidin", "Cyanidin"],
    "Strawberry": ["Ellagic acid", "Fisetin", "Anthocyanins", "Pelargonidin", "Kaempferol",
                   "Quercetin", "Catechin", "Gallic acid", "Ascorbic acid"],
    "Blueberry": ["Anthocyanins", "Pterostilbene", "Resveratrol", "Chlorogenic acid", "Quercetin",
                  "Myricetin", "Kaempferol", "Delphinidin", "Malvidin", "Cyanidin",
                  "Proanthocyanidins"],
    "Raspberry": ["Ellagic acid", "Raspberry ketone", "Anthocyanins", "Cyanidin", "Quercetin",
                  "Kaempferol", "Gallic acid", "Salicylic acid"],
    "Cherry": ["Anthocyanins", "Cyanidin", "Quercetin", "Kaempferol", "Catechin", "Melatonin",
               "Chlorogenic acid", "Perillyl alcohol"],
    "Peach": ["Chlorogenic acid", "Neochlorogenic acid", "Catechin", "Epicatechin", "Quercetin",
              "Cyanidin", "beta Carotene", "Lutein"],
    "Plum": ["Chlorogenic acid", "Neochlorogenic acid", "Rutin", "Quercetin", "Anthocyanins",
             "Cyanidin", "Catechin", "Proanthocyanidins"],
    "Apricot": ["beta Carotene", "Lycopene", "Lutein", "Chlorogenic acid", "Catechin", "Quercetin",
                "Rutin", "Amygdalin"],
    "Avocado": ["Lutein", "Zeaxanthin", "beta-Sitosterol", "Glutathione", "Persin", "Oleic acid",
                "alpha-Tocopherol", "Chlorogenic acid", "Epicatechin"],
    "Coconut": ["Lauric acid", "Caprylic acid", "Decanoic acid", "Ferulic acid", "Caffeic acid",
                "Gallic acid", "alpha-Tocopherol"],
    "Guava": ["Quercetin", "Lycopene", "Gallic acid", "Ellagic acid", "Catechin", "Kaempferol",
              "Myricetin", "Ursolic acid", "Oleanolic acid", "beta-Caryophyllene",
              "Ascorbic acid"],
    "Passion fruit": ["Piceatannol", "Quercetin", "Luteolin", "Apigenin", "Vitexin", "Isovitexin",
                      "Orientin", "beta Carotene", "Harmane"],
    "Kiwi": ["Ascorbic acid", "Lutein", "Quercetin", "Chlorogenic acid", "Caffeic acid",
             "Catechin", "Ursolic acid"],
    "Pomegranate": ["Punicalagin", "Ellagic acid", "Gallic acid", "Anthocyanins", "Delphinidin",
                    "Cyanidin", "Hydrolyzable Tannins", "Punicic acid", "Ursolic acid", "Quercetin", "Catechin"],
    "Fig": ["Psoralen", "Bergapten", "Methoxsalen", "Rutin", "Quercetin", "Chlorogenic acid", "Catechin",
            "Luteolin"],
    "Date": ["Gallic acid", "Ferulic acid", "Caffeic acid", "Quercetin", "Luteolin", "Apigenin",
             "Catechin", "Procyanidin", "Lutein", "beta Carotene"],
    "Jackfruit": ["Artocarpin", "Morin", "Lutein", "beta Carotene", "Ascorbic acid"],
    "Soursop": ["Annonacin", "Quercetin", "Kaempferol", "Gallic acid", "Chlorogenic acid"],
    "Tamarind": ["Lupeol", "Procyanidin", "Catechin", "Epicatechin", "Taxifolin", "Apigenin",
                 "Luteolin", "Naringenin", "Vitexin", "Tartaric acid"],
    "Mulberry": ["1-Deoxynojirimycin", "Resveratrol", "Oxyresveratrol", "Cyanidin", "Rutin",
                 "Quercetin", "Chlorogenic acid", "Morin"],
    "Cranberry": ["Proanthocyanidins", "Quercetin", "Myricetin", "Ursolic acid", "Anthocyanins",
                  "Cyanidin", "Peonidin", "Resveratrol"],
    "Blackberry": ["Anthocyanins", "Cyanidin", "Ellagic acid", "Gallic acid", "Quercetin",
                   "Kaempferol", "Catechin"],
    "Dragon fruit": ["Betanin", "Gallic acid", "Quercetin", "Kaempferol", "Ascorbic acid"],
    "Starfruit": ["Proanthocyanidins", "Epicatechin", "Catechin", "Quercetin", "Gallic acid",
                  "Ascorbic acid"],
    "Lychee": ["Epicatechin", "Procyanidin", "Rutin", "Quercetin", "Catechin", "Gallic acid",
               "Ascorbic acid"],
    "African star apple": _STAR_APPLE,
    "Agbalumo": _STAR_APPLE,
    # Vegetables
    "Tomato": ["Lycopene", "beta Carotene", "Lutein", "Naringenin", "Chlorogenic acid", "Rutin",
               "Quercetin", "Kaempferol", "Caffeic acid", "Tomatine"],
    "Carrot": ["beta Carotene", "alpha-Carotene", "Lutein", "Falcarinol", "Chlorogenic acid",
               "Caffeic acid", "Luteolin"],
    "Spinach": ["Lutein", "Zeaxanthin", "beta Carotene", "Neoxanthin", "Kaempferol", "Quercetin",
                "Spinacetin", "Patuletin", "3-O-beta-D-glucopyranosyl-spinasterol", "Thioctic acid", "Folic acid"],
    "Kale": ["Kaempferol", "Quercetin", "Isorhamnetin", "Lutein", "Zeaxanthin", "beta Carotene",
             "Sulforaphane", "Indole-3-carbinol", "Glucoraphanin"],
    "Broccoli": ["Sulforaphane", "Indole-3-carbinol", "3,3'-Diindolylmethane", "Glucoraphanin",
                 "Kaempferol", "Quercetin", "Lutein", "beta Carotene", "Ascorbic acid"],
    "Cauliflower": ["Sulforaphane", "Indole-3-carbinol", "Glucobrassicin", "Kaempferol",
                    "Quercetin", "Ascorbic acid"],
    "Cabbage": ["Sulforaphane", "Indole-3-carbinol", "Allyl isothiocyanate", "Sinigrin",
                "Anthocyanins", "Kaempferol", "Quercetin", "Ascorbic acid"],
    "Onion": ["Quercetin", "Kaempferol", "Isorhamnetin", "Fisetin", "Anthocyanins", "Cyanidin",
              "Spiraeoside"],
    "Garlic": ["Allicin", "Alliin", "Diallyl disulfide", "Diallyl trisulfide", "Diallyl sulfide",
               "S-Allylcysteine", "Ajoene", "Quercetin"],
    "Ginger": _GINGER,
    "Pepper": _CHILI,
    "Chili": _CHILI,
    "Chili pepper": _CHILI,
    "Sweet pepper": _BELL_PEPPER,
    "Bell pepper": _BELL_PEPPER,
    "Sweet potato": ["beta Carotene", "Chlorogenic acid", "Caffeic acid", "Anthocyanins",
                     "Peonidin", "Cyanidin", "Quercetin"],
    "Yam": ["Diosgenin", "Dioscin", "Allantoin", "beta-Sitosterol", "Batatasin"],
    "Cassava": ["Linamarin", "Lotaustralin", "beta Carotene", "Lutein", "Rutin", "Kaempferol"],
    "Okra": ["Quercetin", "Isoquercitrin", "Hyperoside", "Rutin", "Catechin", "Epicatechin",
             "Procyanidin", "Myricetin"],
    "Eggplant": ["Nasunin", "Delphinidin", "Anthocyanins", "Chlorogenic acid", "Caffeic acid",
                 "Solasodine"],
    "Cucumber": ["Cucurbitacin B", "Cucurbitacin C", "Lariciresinol", "Pinoresinol",
                 "Secoisolariciresinol", "Kaempferol", "Quercetin", "Apigenin", "Luteolin",
                 "Fisetin"],
    "Zucchini": ["Lutein", "Zeaxanthin", "beta Carotene", "Quercetin", "Kaempferol"],
    "Green beans": _GREEN_BEAN,
    "Green bean": _GREEN_BEAN,
    "Peas": _PEA,
    "Pea": _PEA,
    "Corn": ["Lutein", "Zeaxanthin", "beta-Cryptoxanthin", "Ferulic acid", "p-Coumaric acid",
             "Anthocyanins", "Cyanidin", "beta-Sitosterol"],
    "Beetroot": ["Betanin", "Betaine", "Vulgaxanthin", "Ferulic acid", "Rutin", "Epicatechin"],
    "Radish": ["Sulforaphene", "Sulforaphane", "Glucoraphenin", "Kaempferol", "Anthocyanins",
               "Pelargonidin"],
    "Turnip": ["Phenethyl isothiocyanate", "Gluconasturtiin", "Indole-3-carbinol", "Kaempferol",
               "Quercetin"],
    "Celery": ["Apigenin", "Luteolin", "Apiin", "3-n-Butylphthalide", "Sedanolide", "Bergapten", "Methoxsalen",
               "Psoralen", "Chlorogenic acid", "Caffeic acid", "Ferulic acid"],
    "Leek": ["Kaempferol", "Quercetin", "Lutein", "Zeaxanthin", "Allicin"],
    "Asparagus": ["Rutin", "Quercetin", "Kaempferol", "Isorhamnetin", "Protodioscin", "Saponins",
                  "Glutathione", "Asparagusic acid"],
    "Artichoke": ["Cynarin", "Cynaropicrin", "Chlorogenic acid", "Luteolin", "Cynaroside", "Apigenin",
                  "Caffeic acid", "Scolymoside"],
    "Brussels sprouts": ["Sulforaphane", "Indole-3-carbinol", "Sinigrin", "Glucobrassicin",
                         "Kaempferol", "Quercetin", "Ascorbic acid"],
    "Pumpkin": ["beta Carotene", "alpha-Carotene", "Lutein", "Zeaxanthin", "Cucurbitin",
                "beta-Sitosterol", "Squalene", "alpha-Tocopherol"],
    "Bitter leaf": ["Vernolide", "Vernodalin", "Vernodalol", "Luteolin", "Quercetin", "Apigenin",
                    "Kaempferol", "Stigmasterol", "Saponins"],
    "Ugwu": _FLUTED_PUMPKIN,
    "Fluted pumpkin": _FLUTED_PUMPKIN,
    "Scent leaf": _SCENT_LEAF,
    "African basil": _SCENT_LEAF,
    "Uziza": _UZIZA,
    "Uziza leaf": _UZIZA,
    "Utazi": _UTAZI,
    "Utazi leaf": _UTAZI,
    # African plants
    "Moringa": _MORINGA,
    "Neem": _NEEM,
    "Dogonyaro": _NEEM,
    "Tiger nut": ["Oleic acid", "alpha-Tocopherol", "beta-Sitosterol", "Stigmasterol",
                  "Ferulic acid", "p-Coumaric acid", "Vanillin", "Caffeic acid"],
    "Baobab": ["Ascorbic acid", "Procyanidin", "Catechin", "Epicatechin", "Quercetin", "Rutin",
               "Gallic acid", "Lupeol", "beta-Sitosterol"],
    "Hibiscus": _HIBISCUS,
    "Zobo": _HIBISCUS,
    "Roselle": _HIBISCUS,
    "Shea": _SHEA,
    "Shea butter tree": _SHEA,
    "Kola nut": ["Caffeine", "Theobromine", "Catechin", "Epicatechin", "Procyanidin"],
    "Bitter kola": ["Kolaviron", "Amentoflavone", "Caffeine", "Quercetin"],
    "Negro pepper": _NEGRO_PEPPER,
    "Uda": _NEGRO_PEPPER,
    "Ehuru": _EHURU,
    "African nutmeg": _EHURU,
    "Ogiri": ["Oleic acid", "Linoleic acid", "Palmitic acid"],
    "Monkey cola": _MONKEY_KOLA,
    "Monkey kola": _MONKEY_KOLA,
    "Bush mango": _BUSH_MANGO,
    "Ogbono": _BUSH_MANGO,
    "Black seed": ["Thymoquinone", "Thymohydroquinone", "alpha-Hederin", "Carvacrol",
                   "p-Cymene"],
    # Herbs and spices
    "Turmeric": ["Curcumin", "Bisdemethoxycurcumin", "Demethoxycurcumin", "Turmerone",
                 "Zingiberene", "beta-Elemene"],
    "Cinnamon": ["Cinnamaldehyde", "cinnamon oil, bark", "Eugenol", "Coumarin", "Cinnamic acid", "Procyanidin",
                 "Catechin", "Linalool", "Camphor"],
    "Cloves": ["Eugenol", "Eugenyl acetate", "beta-Caryophyllene", "Gallic acid",
               "Oleanolic acid", "Kaempferol", "Quercetin", "Ellagic acid"],
    "Cardamom": ["Eucalyptol", "Linalool", "Limonene", "Geraniol", "Terpinen-4-ol",
                 "alpha-Terpineol", "alpha-Terpinyl acetate"],
    "Black pepper": ["Piperine", "beta-Caryophyllene", "Limonene", "alpha-Pinene", "beta-Pinene",
                     "Sabinene", "Linalool"],
    "Cumin": ["Cuminaldehyde", "gamma-Terpinene", "p-Cymene", "beta-Pinene", "Apigenin",
              "Luteolin"],
    "Coriander": ["Linalool", "Geraniol", "Camphor", "alpha-Pinene", "Quercetin", "Kaempferol",
                  "Rutin", "Caffeic acid", "Chlorogenic acid"],
    "Fenugreek": ["Trigonelline", "Diosgenin", "4-Hydroxyisoleucine", "Vitexin", "Isovitexin",
                  "Apigenin", "Luteolin", "Quercetin", "Saponins"],
    "Thyme": ["Thymol", "Carvacrol", "p-Cymene", "gamma-Terpinene", "Rosmarinic acid", "Luteolin",
              "Apigenin", "Linalool", "Eriodictyol"],
    "Rosemary": ["Rosmarinic acid", "Carnosic acid", "Carnosol", "Ursolic acid", "Oleanolic acid",
                 "Betulinic acid", "Eucalyptol", "Camphor", "alpha-Pinene"],
    "Sage": ["Rosmarinic acid", "Carnosic acid", "Carnosol", "Thujone", "Camphor", "Eucalyptol",
             "Ursolic acid", "Luteolin", "Apigenin"],
    "Oregano": ["Carvacrol", "Thymol", "Rosmarinic acid", "p-Cymene", "gamma-Terpinene",
                "Apigenin", "Luteolin", "Caffeic acid"],
    "Basil": ["Eugenol", "Linalool", "Estragole", "Methyleugenol", "Rosmarinic acid",
              "Ursolic acid", "Apigenin", "Orientin", "beta-Caryophyllene", "Eucalyptol"],
    "Mint": _PEPPERMINT,
    "Peppermint": _PEPPERMINT,
    "Parsley": ["Apigenin", "Apiin", "Myristicin", "Luteolin", "Kaempferol", "Lutein",
                "beta Carotene", "Bergapten", "Methoxsalen", "Psoralen", "Ascorbic acid"],
    "Dill": ["Carvone", "Limonene", "alpha-Phellandrene", "Dillapiole", "Quercetin",
             "Kaempferol", "Isorhamnetin"],
    "Bay leaf": ["Eucalyptol", "Linalool", "Eugenol", "Methyleugenol", "Costunolide", "Catechin",
                 "Kaempferol"],
    "Nutmeg": ["Myristicin", "Elemicin", "Safrole", "Eugenol", "Macelignan", "Sabinene",
               "alpha-Pinene", "Terpinen-4-ol", "Limonene"],
    "Saffron": ["Crocin", "Crocetin", "Safranal", "Picrocrocin", "Kaempferol"],
    "Vanilla": ["Vanillin", "4-Hydroxybenzaldehyde", "Vanillic acid"],
    "Anise": ["Anethole", "Estragole", "Anisaldehyde", "Linalool", "Rutin", "Chlorogenic acid",
              "Luteolin", "Apigenin"],
    "Fennel": ["Anethole", "Estragole", "Fenchone", "Limonene", "alpha-Pinene", "Quercetin",
               "Rosmarinic acid", "Chlorogenic acid"],
    # Teas and drinks
    "Green tea": ["Epigallocatechin gallate", "Epigallocatechin", "Epicatechin gallate",
                  "Epicatechin", "Catechin", "Caffeine", "Theanine", "Quercetin", "Kaempferol",
                  "Myricetin", "Gallic acid"],
    "Black tea": ["Theaflavin", "theaflavin-3,3'-digallate", "Thearubigins", "Caffeine", "Theanine", "Epigallocatechin gallate",
                  "Catechin", "Quercetin", "Kaempferol", "Gallic acid"],
    "White tea": ["Epigallocatechin gallate", "Epicatechin", "Catechin", "Caffeine", "Theanine",
                  "Quercetin", "Gallic acid"],
    "Chamomile": ["Apigenin", "Luteolin", "Quercetin", "Bisabolol", "Chamazulene",
                  "Chlorogenic acid", "Umbelliferone", "Herniarin", "Patuletin"],
    "Rooibos": ["Aspalathin", "Nothofagin", "Orientin", "Isoorientin", "Vitexin", "Isovitexin",
                "Rutin", "Quercetin", "Luteolin", "Hyperoside"],
    "Hibiscus tea": _HIBISCUS,
    "Ginger tea": _GINGER,
    "Moringa tea": _MORINGA,
    "Peppermint tea": _PEPPERMINT,
}

# Common name (lowercase) → exact CTD chemical_name, where the two differ by
# more than letter case.
CTD_ALIASES: dict[str, str] = {
    "alpha-phellandrene": "alpha phellandrene",
    "beta-caryophyllene": "caryophyllene",
    "beta-sitosterol": "Sitosterols",
    "bergapten": "5-Methoxypsoralen",
    "harmane": "harman",
    "isoorientin": "homoorientin",
    "isorhamnetin": "isorhamnetin 3-O-glucoside",
    "p-cymene": "4-cymene",
    "protodioscin": "methyl protodioscin",
    "terpinen-4-ol": "terpinenol-4",
    "thujone": "beta-thujone",
    "turmerone": "ar-turmerone",
    "zeaxanthin": "Zeaxanthins",
}


def _norm(name: str) -> str:
    return " ".join(name.lower().split())


def get_client() -> Client:
    load_dotenv(ROOT / ".env")
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise SystemExit("ERROR: SUPABASE_URL and SUPABASE_KEY must be set in .env")
    return create_client(url, key)


def fetch_all(client: Client, table: str, columns: str) -> list[dict[str, Any]]:
    """Every row of a table; PostgREST caps a single response at 1,000 rows."""
    rows: list[dict[str, Any]] = []
    start = 0
    while True:
        page = (
            client.table(table)
            .select(columns)
            .order("id")
            .range(start, start + PAGE_SIZE - 1)
            .execute()
            .data
            or []
        )
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        start += PAGE_SIZE


def resolve_chemicals(
    ctd_names: Iterable[str],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Invert PLANTS into CTD chemical name → plants, plus unresolved → plants."""
    by_lower = {_norm(n): n for n in ctd_names}
    resolved: dict[str, list[str]] = {}
    unresolved: dict[str, list[str]] = {}
    for plant, chemicals in PLANTS.items():
        for chemical in chemicals:
            key = _norm(chemical)
            ctd_name = by_lower.get(_norm(CTD_ALIASES.get(key, key)))
            target = resolved.setdefault(ctd_name, []) if ctd_name else unresolved.setdefault(chemical, [])
            if plant not in target:
                target.append(plant)
    return resolved, unresolved


def plan_upserts(
    resolved: dict[str, list[str]],
    existing: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(updates to existing rows, brand-new rows) — only rows that gain plants."""
    by_lower = {_norm(r["phytochemical_name"]): r for r in existing}
    updates: list[dict[str, Any]] = []
    inserts: list[dict[str, Any]] = []
    for ctd_name, plants in sorted(resolved.items()):
        row = by_lower.get(_norm(ctd_name))
        if row is None:
            inserts.append(
                {
                    "phytochemical_name": ctd_name,
                    "fruit_vegetables": plants,
                    "primary_sources": plants[:PRIMARY_SOURCES_PER_ROW],
                    "chemical_class": None,
                }
            )
            continue
        current = [str(fv) for fv in row.get("fruit_vegetables") or []]
        seen = {_norm(fv) for fv in current}
        added = [p for p in plants if _norm(p) not in seen]
        if added:
            updates.append(
                {"phytochemical_name": row["phytochemical_name"], "fruit_vegetables": current + added}
            )
    return updates, inserts


def distinct_plants(rows: Iterable[dict[str, Any]]) -> set[str]:
    return {_norm(str(fv)) for r in rows for fv in r.get("fruit_vegetables") or [] if str(fv).strip()}


def upsert(client: Client, rows: list[dict[str, Any]]) -> None:
    for i in range(0, len(rows), BATCH_SIZE):
        client.table(SOURCES_TABLE).upsert(
            rows[i : i + BATCH_SIZE], on_conflict="phytochemical_name"
        ).execute()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="report the plan without writing")
    args = parser.parse_args()

    client = get_client()
    ctd_names = {r["chemical_name"] for r in fetch_all(client, CTD_TABLE, "chemical_name")}
    print(f"CTD: {len(ctd_names):,} distinct chemicals with gene interactions")

    existing = fetch_all(client, SOURCES_TABLE, "phytochemical_name,fruit_vegetables")
    plants_before = distinct_plants(existing)
    print(f"phytochemical_sources: {len(existing):,} rows, {len(plants_before):,} distinct plants")

    resolved, unresolved = resolve_chemicals(ctd_names)
    updates, inserts = plan_upserts(resolved, existing)

    plants_mapped = {_norm(p) for plants in resolved.values() for p in plants}
    new_plants = sorted(plants_mapped - plants_before)
    unmapped = sorted(p for p in PLANTS if _norm(p) not in plants_mapped)

    print(f"Resolved {len(resolved)} compounds to CTD names; {len(unresolved)} not in CTD (skipped)")
    for chemical, plants in sorted(unresolved.items(), key=lambda kv: kv[0].lower()):
        print(f"  - {chemical}  ({', '.join(plants[:4])}{', ...' if len(plants) > 4 else ''})")
    if unmapped:
        print(f"WARNING: {len(unmapped)} plant(s) have no CTD-backed compound: {', '.join(unmapped)}")
    print(f"Plan: {len(updates)} existing rows gain plants, {len(inserts)} new compound rows")
    print(f"New plants: {len(new_plants)}")

    if args.dry_run:
        print("  " + ", ".join(new_plants))
        print("Dry run: nothing written.")
        return 0

    try:
        upsert(client, updates)
        upsert(client, inserts)
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero
        print(f"ERROR: upsert failed: {exc}")
        return 1

    after = fetch_all(client, SOURCES_TABLE, "phytochemical_name,fruit_vegetables")
    plants_after = distinct_plants(after)
    print(
        f"Done: {len(after):,} rows, {len(plants_after):,} distinct plants "
        f"(+{len(plants_after) - len(plants_before)} new plants)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
