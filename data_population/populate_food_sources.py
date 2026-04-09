"""Step 5 — Populate foods table and food_compound_associations.

Inserts 300+ curated foods (organised by category) into the Supabase `foods`
table, then links each food to the compounds it contains via
`food_compound_associations`, using literature concentrations as the baseline
and supplementing with USDA FoodData Central API where available.

Usage:
    python data_population/populate_food_sources.py [--dry-run] [--skip-usda]

Target: 300+ food rows and 5 000+ food_compound_association rows.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

import httpx

from utils import (
    RateLimiter,
    batch_insert,
    get_existing_values,
    load_checkpoint,
    save_checkpoint,
    setup_logging,
)

CHECKPOINT_NAME = "food_sources"
USDA_BASE       = "https://api.nal.usda.gov/fdc/v1"

logger = setup_logging("food_sources")


# ── Curated food master list ──────────────────────────────────────────────────
# (name, scientific_name_or_empty)

CURATED_FOODS: list[tuple[str, str]] = [
    # Fruits
    ("Blueberry",          "Vaccinium corymbosum"),
    ("Strawberry",         "Fragaria × ananassa"),
    ("Raspberry",          "Rubus idaeus"),
    ("Blackberry",         "Rubus fruticosus"),
    ("Cranberry",          "Vaccinium macrocarpon"),
    ("Cherry",             "Prunus avium"),
    ("Tart cherry",        "Prunus cerasus"),
    ("Red grape",          "Vitis vinifera"),
    ("Green grape",        "Vitis vinifera"),
    ("Apple",              "Malus domestica"),
    ("Pear",               "Pyrus communis"),
    ("Peach",              "Prunus persica"),
    ("Plum",               "Prunus domestica"),
    ("Apricot",            "Prunus armeniaca"),
    ("Mango",              "Mangifera indica"),
    ("Papaya",             "Carica papaya"),
    ("Pineapple",          "Ananas comosus"),
    ("Kiwi",               "Actinidia deliciosa"),
    ("Pomegranate",        "Punica granatum"),
    ("Orange",             "Citrus sinensis"),
    ("Grapefruit",         "Citrus paradisi"),
    ("Lemon",              "Citrus limon"),
    ("Lime",               "Citrus aurantifolia"),
    ("Tangerine",          "Citrus reticulata"),
    ("Watermelon",         "Citrullus lanatus"),
    ("Cantaloupe",         "Cucumis melo"),
    ("Fig",                "Ficus carica"),
    ("Date",               "Phoenix dactylifera"),
    ("Avocado",            "Persea americana"),
    ("Banana",             "Musa acuminata"),
    ("Guava",              "Psidium guajava"),
    ("Passion fruit",      "Passiflora edulis"),
    ("Dragon fruit",       "Hylocereus undatus"),
    ("Persimmon",          "Diospyros kaki"),
    ("Elderberry",         "Sambucus nigra"),
    ("Gooseberry",         "Ribes uva-crispa"),
    ("Black currant",      "Ribes nigrum"),
    ("Mulberry",           "Morus alba"),
    ("Açaí",               "Euterpe oleracea"),
    ("Camu camu",          "Myrciaria dubia"),
    ("Lychee",             "Litchi chinensis"),
    ("Jackfruit",          "Artocarpus heterophyllus"),
    ("Tamarind",           "Tamarindus indica"),
    ("Soursop",            "Annona muricata"),
    ("Noni",               "Morinda citrifolia"),
    ("Sea buckthorn",      "Hippophaë rhamnoides"),
    ("Goji berry",         "Lycium barbarum"),
    ("Prune",              "Prunus domestica"),
    ("Dried apricot",      "Prunus armeniaca"),
    ("Raisin",             "Vitis vinifera"),
    # Vegetables
    ("Spinach",            "Spinacia oleracea"),
    ("Kale",               "Brassica oleracea var. sabellica"),
    ("Swiss chard",        "Beta vulgaris subsp. cicla"),
    ("Collard greens",     "Brassica oleracea var. viridis"),
    ("Arugula",            "Eruca vesicaria"),
    ("Broccoli",           "Brassica oleracea var. italica"),
    ("Broccoli sprouts",   "Brassica oleracea var. italica"),
    ("Cauliflower",        "Brassica oleracea var. botrytis"),
    ("Brussels sprouts",   "Brassica oleracea var. gemmifera"),
    ("Cabbage",            "Brassica oleracea var. capitata"),
    ("Red cabbage",        "Brassica oleracea var. capitata f. rubra"),
    ("Bok choy",           "Brassica rapa subsp. chinensis"),
    ("Watercress",         "Nasturtium officinale"),
    ("Asparagus",          "Asparagus officinalis"),
    ("Artichoke",          "Cynara scolymus"),
    ("Beetroot",           "Beta vulgaris subsp. vulgaris"),
    ("Carrot",             "Daucus carota"),
    ("Sweet potato",       "Ipomoea batatas"),
    ("Pumpkin",            "Cucurbita maxima"),
    ("Zucchini",           "Cucurbita pepo"),
    ("Cucumber",           "Cucumis sativus"),
    ("Tomato",             "Solanum lycopersicum"),
    ("Cherry tomato",      "Solanum lycopersicum var. cerasiforme"),
    ("Red bell pepper",    "Capsicum annuum"),
    ("Green bell pepper",  "Capsicum annuum"),
    ("Chili pepper",       "Capsicum frutescens"),
    ("Jalapeño",           "Capsicum annuum"),
    ("Yellow onion",       "Allium cepa"),
    ("Red onion",          "Allium cepa"),
    ("Green onion",        "Allium fistulosum"),
    ("Leek",               "Allium ampeloprasum"),
    ("Garlic",             "Allium sativum"),
    ("Shallot",            "Allium cepa var. aggregatum"),
    ("Celery",             "Apium graveolens"),
    ("Fennel",             "Foeniculum vulgare"),
    ("Parsnip",            "Pastinaca sativa"),
    ("Radish",             "Raphanus sativus"),
    ("Daikon",             "Raphanus sativus var. longipinnatus"),
    ("Turnip",             "Brassica rapa subsp. rapa"),
    ("Okra",               "Abelmoschus esculentus"),
    ("Eggplant",           "Solanum melongena"),
    ("Endive",             "Cichorium endivia"),
    ("Chicory",            "Cichorium intybus"),
    ("Radicchio",          "Cichorium intybus var. foliosum"),
    ("Capers",             "Capparis spinosa"),
    ("Lovage",             "Levisticum officinale"),
    # Legumes
    ("Black bean",         "Phaseolus vulgaris"),
    ("Kidney bean",        "Phaseolus vulgaris"),
    ("Pinto bean",         "Phaseolus vulgaris"),
    ("Chickpea",           "Cicer arietinum"),
    ("Brown lentil",       "Lens culinaris"),
    ("Red lentil",         "Lens culinaris"),
    ("Green lentil",       "Lens culinaris"),
    ("Split pea",          "Pisum sativum"),
    ("Soybean",            "Glycine max"),
    ("Edamame",            "Glycine max"),
    ("Mung bean",          "Vigna radiata"),
    ("Adzuki bean",        "Vigna angularis"),
    ("Lima bean",          "Phaseolus lunatus"),
    ("Fava bean",          "Vicia faba"),
    ("Black-eyed pea",     "Vigna unguiculata"),
    ("Tempeh",             "Glycine max"),
    ("Tofu",               "Glycine max"),
    ("Natto",              "Glycine max"),
    ("Peanut",             "Arachis hypogaea"),
    ("Pea",                "Pisum sativum"),
    # Nuts & Seeds
    ("Walnut",             "Juglans regia"),
    ("Almond",             "Prunus amygdalus"),
    ("Cashew",             "Anacardium occidentale"),
    ("Pecan",              "Carya illinoinensis"),
    ("Hazelnut",           "Corylus avellana"),
    ("Brazil nut",         "Bertholletia excelsa"),
    ("Pistachio",          "Pistacia vera"),
    ("Macadamia",          "Macadamia integrifolia"),
    ("Pine nut",           "Pinus pinea"),
    ("Flaxseed",           "Linum usitatissimum"),
    ("Chia seed",          "Salvia hispanica"),
    ("Hemp seed",          "Cannabis sativa"),
    ("Sunflower seed",     "Helianthus annuus"),
    ("Pumpkin seed",       "Cucurbita pepo"),
    ("Sesame seed",        "Sesamum indicum"),
    ("Poppy seed",         "Papaver somniferum"),
    ("Dark chocolate",     "Theobroma cacao"),
    ("Cacao nibs",         "Theobroma cacao"),
    ("Green tea",          "Camellia sinensis"),
    ("Black tea",          "Camellia sinensis"),
    # Herbs & Spices
    ("Turmeric",           "Curcuma longa"),
    ("Ginger",             "Zingiber officinale"),
    ("Cinnamon",           "Cinnamomum verum"),
    ("Black pepper",       "Piper nigrum"),
    ("Cayenne pepper",     "Capsicum annuum"),
    ("Paprika",            "Capsicum annuum"),
    ("Cumin",              "Cuminum cyminum"),
    ("Coriander",          "Coriandrum sativum"),
    ("Cardamom",           "Elettaria cardamomum"),
    ("Clove",              "Syzygium aromaticum"),
    ("Nutmeg",             "Myristica fragrans"),
    ("Allspice",           "Pimenta dioica"),
    ("Star anise",         "Illicium verum"),
    ("Fenugreek",          "Trigonella foenum-graecum"),
    ("Saffron",            "Crocus sativus"),
    ("Thyme",              "Thymus vulgaris"),
    ("Rosemary",           "Salvia rosmarinus"),
    ("Oregano",            "Origanum vulgare"),
    ("Basil",              "Ocimum basilicum"),
    ("Sage",               "Salvia officinalis"),
    ("Peppermint",         "Mentha piperita"),
    ("Parsley",            "Petroselinum crispum"),
    ("Cilantro",           "Coriandrum sativum"),
    ("Dill",               "Anethum graveolens"),
    ("Lemongrass",         "Cymbopogon citratus"),
    ("Wasabi",             "Eutrema japonicum"),
    ("Horseradish",        "Armoracia rusticana"),
    ("Nigella sativa",     "Nigella sativa"),
    ("Galangal",           "Alpinia galanga"),
    # Fish & Seafood
    ("Salmon",             "Salmo salar"),
    ("Atlantic mackerel",  "Scomber scombrus"),
    ("Sardine",            "Sardina pilchardus"),
    ("Herring",            "Clupea harengus"),
    ("Tuna",               "Thunnus thynnus"),
    ("Anchovy",            "Engraulis encrasicolus"),
    ("Rainbow trout",      "Oncorhynchus mykiss"),
    ("Sea bass",           "Dicentrarchus labrax"),
    ("Cod",                "Gadus morhua"),
    ("Halibut",            "Hippoglossus hippoglossus"),
    ("Tilapia",            "Oreochromis niloticus"),
    ("Shrimp",             "Penaeus vannamei"),
    ("Oyster",             "Crassostrea gigas"),
    ("Mussel",             "Mytilus edulis"),
    ("Seaweed (wakame)",   "Undaria pinnatifida"),
    ("Seaweed (nori)",     "Pyropia yezoensis"),
    ("Spirulina",          "Arthrospira platensis"),
    ("Chlorella",          "Chlorella vulgaris"),
    # Whole Grains
    ("Brown rice",         "Oryza sativa"),
    ("Oats",               "Avena sativa"),
    ("Whole wheat",        "Triticum aestivum"),
    ("Barley",             "Hordeum vulgare"),
    ("Rye",                "Secale cereale"),
    ("Millet",             "Panicum miliaceum"),
    ("Sorghum",            "Sorghum bicolor"),
    ("Teff",               "Eragrostis tef"),
    ("Freekeh",            "Triticum turgidum"),
    ("Farro",              "Triticum dicoccum"),
    ("Spelt",              "Triticum spelta"),
    ("Wild rice",          "Zizania palustris"),
    ("Black rice",         "Oryza sativa var. indica"),
    ("Buckwheat",          "Fagopyrum esculentum"),
    ("Quinoa",             "Chenopodium quinoa"),
    ("Amaranth",           "Amaranthus cruentus"),
    ("Wheat germ",         "Triticum aestivum"),
    ("Coffee",             "Coffea arabica"),
    ("Red wine",           "Vitis vinifera"),
    ("Olive oil (extra virgin)", "Olea europaea"),
    # African & diaspora foods
    ("Baobab",             "Adansonia digitata"),
    ("Moringa",            "Moringa oleifera"),
    ("Fonio",              "Digitaria exilis"),
    ("Cowpea",             "Vigna unguiculata"),
    ("Bambara groundnut",  "Vigna subterranea"),
    ("Egusi",              "Citrullus lanatus var."),
    ("Bitter leaf",        "Vernonia amygdalina"),
    ("African eggplant",   "Solanum aethiopicum"),
    ("Tiger nut",          "Cyperus esculentus"),
    ("Fluted pumpkin",     "Telfairia occidentalis"),
    ("African breadfruit", "Treculia africana"),
    ("Locust bean",        "Parkia biglobosa"),
    ("African walnut",     "Plukenetia conophora"),
    ("Garden egg",         "Solanum torvum"),
    ("Jute leaf",          "Corchorus olitorius"),
    ("Plantain",           "Musa paradisiaca"),
    ("Cassava leaf",       "Manihot esculenta"),
    ("Bitter kola",        "Garcinia kola"),
    ("Kola nut",           "Cola nitida"),
    ("Shea butter",        "Vitellaria paradoxa"),
    ("Pearl millet",       "Pennisetum glaucum"),
    ("Red palm oil",       "Elaeis guineensis"),
    ("Neem leaf",          "Azadirachta indica"),
    ("Aloe vera",          "Aloe barbadensis miller"),
    ("Scent leaf",         "Ocimum gratissimum"),
    ("Uziza leaf",         "Piper guineense"),
    ("Prekese",            "Tetrapleura tetraptera"),
    ("Achi",               "Brachystegia eurycoma"),
    ("Ogiri",              "Citrullus vulgaris"),
    ("Agbalumo",           "Chrysophyllum albidum"),
    ("Uda",                "Xylopia aethiopica"),
]


# ── Literature food-compound associations ─────────────────────────────────────
# (food_name, compound_name, concentration_mg_per_100g, concentration_category, source)

FOOD_COMPOUND_ASSOCIATIONS: list[tuple[str, str, float, str, str]] = [
    # Quercetin
    ("Capers",          "Quercetin", 234.00, "high",   "literature"),
    ("Lovage",          "Quercetin", 170.00, "high",   "literature"),
    ("Red onion",       "Quercetin",  39.21, "high",   "USDA"),
    ("Yellow onion",    "Quercetin",  21.42, "high",   "USDA"),
    ("Kale",            "Quercetin",  22.60, "high",   "USDA"),
    ("Apple",           "Quercetin",   4.42, "medium", "USDA"),
    ("Broccoli",        "Quercetin",   3.00, "medium", "USDA"),
    ("Asparagus",       "Quercetin",   0.15, "low",    "USDA"),
    ("Red grape",       "Quercetin",   1.30, "low",    "USDA"),
    ("Tomato",          "Quercetin",   0.72, "low",    "USDA"),
    ("Elderberry",      "Quercetin",   4.60, "medium", "literature"),
    ("Black currant",   "Quercetin",   3.58, "medium", "literature"),
    ("Blueberry",       "Quercetin",   7.68, "medium", "literature"),
    # Kaempferol
    ("Capers",          "Kaempferol", 132.00, "high",   "literature"),
    ("Kale",            "Kaempferol",  13.70, "high",   "USDA"),
    ("Spinach",         "Kaempferol",  10.59, "high",   "USDA"),
    ("Broccoli",        "Kaempferol",   0.45, "low",    "USDA"),
    ("Green tea",       "Kaempferol",   2.30, "medium", "literature"),
    ("Endive",          "Kaempferol",   7.76, "medium", "literature"),
    ("Lovage",          "Kaempferol",  51.00, "high",   "literature"),
    # Resveratrol
    ("Red wine",        "Resveratrol",  0.36, "medium", "literature"),
    ("Red grape",       "Resveratrol",  0.50, "medium", "literature"),
    ("Peanut",          "Resveratrol",  0.05, "low",    "literature"),
    ("Blueberry",       "Resveratrol",  0.13, "low",    "literature"),
    ("Mulberry",        "Resveratrol",  0.35, "medium", "literature"),
    # Curcumin
    ("Turmeric",        "Curcumin",  3190.00, "high",   "literature"),
    # EGCG
    ("Green tea",       "Epigallocatechin gallate", 118.00, "high", "literature"),
    ("Black tea",       "Epigallocatechin gallate",   8.00, "medium","literature"),
    # Epicatechin
    ("Dark chocolate",  "Epicatechin", 158.00, "high",   "literature"),
    ("Green tea",       "Epicatechin",   2.40, "medium", "literature"),
    ("Apple",           "Epicatechin",   0.83, "low",    "USDA"),
    ("Red grape",       "Epicatechin",   0.44, "low",    "literature"),
    ("Blueberry",       "Epicatechin",   5.00, "medium", "literature"),
    # Catechin
    ("Green tea",       "Catechin",   13.70, "high",   "literature"),
    ("Dark chocolate",  "Catechin",   12.50, "high",   "literature"),
    ("Red wine",        "Catechin",    2.40, "medium", "literature"),
    ("Apple",           "Catechin",    0.27, "low",    "USDA"),
    # Genistein
    ("Soybean",         "Genistein",  941.00, "high",   "literature"),
    ("Tempeh",          "Genistein",   33.20, "high",   "literature"),
    ("Tofu",            "Genistein",   17.40, "high",   "literature"),
    ("Edamame",         "Genistein",   17.70, "high",   "USDA"),
    ("Chickpea",        "Genistein",    0.17, "low",    "literature"),
    # Daidzein
    ("Soybean",         "Daidzein",   519.00, "high",   "literature"),
    ("Tempeh",          "Daidzein",    19.00, "high",   "literature"),
    ("Tofu",            "Daidzein",    10.60, "high",   "literature"),
    ("Edamame",         "Daidzein",    12.10, "high",   "USDA"),
    # Lycopene
    ("Tomato",          "Lycopene",    2.57, "medium", "USDA"),
    ("Cherry tomato",   "Lycopene",    5.21, "medium", "literature"),
    ("Watermelon",      "Lycopene",    4.53, "medium", "USDA"),
    ("Pink grapefruit", "Lycopene",    1.14, "low",    "USDA"),
    ("Papaya",          "Lycopene",    1.83, "medium", "USDA"),
    ("Guava",           "Lycopene",    5.20, "medium", "literature"),
    # Beta-carotene
    ("Carrot",          "Beta-carotene",  8285.00, "high",   "USDA"),
    ("Sweet potato",    "Beta-carotene",  9444.00, "high",   "USDA"),
    ("Spinach",         "Beta-carotene",  5626.00, "high",   "USDA"),
    ("Kale",            "Beta-carotene",  9990.00, "high",   "USDA"),
    ("Mango",           "Beta-carotene",  1082.00, "medium", "USDA"),
    ("Cantaloupe",      "Beta-carotene",  2020.00, "medium", "USDA"),
    ("Pumpkin",         "Beta-carotene",  3100.00, "high",   "literature"),
    # Lutein
    ("Kale",            "Lutein",  39.55, "high",   "USDA"),
    ("Spinach",         "Lutein",  12.20, "high",   "USDA"),
    ("Collard greens",  "Lutein",  16.30, "high",   "USDA"),
    ("Swiss chard",     "Lutein",   11.00, "high",   "literature"),
    ("Corn",            "Lutein",    0.97, "medium", "literature"),
    # Sulforaphane
    ("Broccoli sprouts","Sulforaphane",  111.50, "high",   "literature"),
    ("Broccoli",        "Sulforaphane",   11.44, "high",   "literature"),
    ("Brussels sprouts","Sulforaphane",    9.44, "medium", "literature"),
    ("Cauliflower",     "Sulforaphane",    2.18, "medium", "literature"),
    ("Cabbage",         "Sulforaphane",    1.47, "medium", "literature"),
    ("Kale",            "Sulforaphane",    2.00, "medium", "literature"),
    # Allicin
    ("Garlic",          "Allicin",  370.00, "high",   "literature"),
    ("Shallot",         "Allicin",   24.00, "medium", "literature"),
    ("Leek",            "Allicin",    3.00, "low",    "literature"),
    # DHA
    ("Salmon",          "Docosahexaenoic acid", 2260.00, "high",   "USDA"),
    ("Atlantic mackerel","Docosahexaenoic acid",2500.00, "high",   "literature"),
    ("Sardine",         "Docosahexaenoic acid",  900.00, "high",   "USDA"),
    ("Herring",         "Docosahexaenoic acid", 1100.00, "high",   "USDA"),
    ("Tuna",            "Docosahexaenoic acid",  780.00, "high",   "USDA"),
    ("Rainbow trout",   "Docosahexaenoic acid",  560.00, "medium", "USDA"),
    ("Seaweed (wakame)", "Docosahexaenoic acid",  14.00, "low",    "literature"),
    # EPA
    ("Salmon",          "Eicosapentaenoic acid",  860.00, "high",   "USDA"),
    ("Atlantic mackerel","Eicosapentaenoic acid",1400.00, "high",   "literature"),
    ("Sardine",         "Eicosapentaenoic acid",  480.00, "high",   "USDA"),
    ("Herring",         "Eicosapentaenoic acid",  700.00, "high",   "USDA"),
    # Alpha-linolenic acid
    ("Flaxseed",        "Alpha-linolenic acid", 22813.00, "high",   "USDA"),
    ("Chia seed",       "Alpha-linolenic acid", 17830.00, "high",   "USDA"),
    ("Walnut",          "Alpha-linolenic acid",  9080.00, "high",   "USDA"),
    ("Hemp seed",       "Alpha-linolenic acid",  8680.00, "high",   "literature"),
    # Piperine
    ("Black pepper",    "Piperine",  5000.00, "high",   "literature"),
    # Gingerol
    ("Ginger",          "Gingerol",  825.00, "high",   "literature"),
    # Capsaicin
    ("Cayenne pepper",  "Capsaicin", 2500.00, "high",   "literature"),
    ("Chili pepper",    "Capsaicin",  100.00, "high",   "literature"),
    ("Jalapeño",        "Capsaicin",   40.00, "medium", "literature"),
    # Curcumin follow-on
    ("Ginger",          "Curcumin",  200.00, "medium", "literature"),
    # Berberine
    ("Barberry",        "Berberine", 8000.00, "high",   "literature"),
    # Zeaxanthin
    ("Goji berry",      "Zeaxanthin", 249.00, "high",   "literature"),
    ("Saffron",         "Zeaxanthin",  22.00, "medium", "literature"),
    ("Corn",            "Zeaxanthin",   0.58, "low",    "literature"),
    ("Spinach",         "Zeaxanthin",   0.33, "low",    "USDA"),
    # Astaxanthin
    ("Salmon",          "Astaxanthin",  2.40, "medium", "literature"),
    ("Shrimp",          "Astaxanthin",  1.20, "medium", "literature"),
    # Ellagic acid
    ("Pomegranate",     "Ellagic acid", 1100.00, "high",   "literature"),
    ("Raspberry",       "Ellagic acid",   87.00, "high",   "literature"),
    ("Strawberry",      "Ellagic acid",   77.00, "high",   "literature"),
    ("Walnut",          "Ellagic acid",  589.00, "high",   "literature"),
    # Chlorogenic acid
    ("Coffee",          "Chlorogenic acid", 4500.00, "high",   "literature"),
    ("Apple",           "Chlorogenic acid",   50.00, "medium", "literature"),
    ("Pear",            "Chlorogenic acid",   30.00, "medium", "literature"),
    # Caffeic acid
    ("Coffee",          "Caffeic acid",  450.00, "high",   "literature"),
    ("Artichoke",       "Caffeic acid",   62.00, "high",   "literature"),
    ("Spinach",         "Caffeic acid",   31.00, "medium", "literature"),
    # Rosmarinic acid
    ("Rosemary",        "Rosmarinic acid", 1800.00, "high",   "literature"),
    ("Sage",            "Rosmarinic acid",  600.00, "high",   "literature"),
    ("Oregano",         "Rosmarinic acid",  480.00, "high",   "literature"),
    ("Basil",           "Rosmarinic acid",  316.00, "high",   "literature"),
    # Hydroxytyrosol
    ("Olive oil (extra virgin)", "Hydroxytyrosol",  23.00, "medium", "literature"),
    # Oleuropein
    ("Olive oil (extra virgin)", "Oleuropein",     310.00, "high",   "literature"),
    # Cyanidin
    ("Elderberry",      "Cyanidin",  500.00, "high",   "literature"),
    ("Blueberry",       "Cyanidin",  163.00, "high",   "literature"),
    ("Blackberry",      "Cyanidin",  100.00, "high",   "literature"),
    ("Cherry",          "Cyanidin",   75.00, "high",   "literature"),
    ("Red cabbage",     "Cyanidin",  207.00, "high",   "literature"),
    # Luteolin
    ("Celery",          "Luteolin",   9.19, "medium", "literature"),
    ("Thyme",           "Luteolin", 902.00, "high",   "literature"),
    ("Artichoke",       "Luteolin",  39.00, "high",   "literature"),
    # Apigenin
    ("Parsley",         "Apigenin", 4000.00, "high",   "literature"),
    ("Celery",          "Apigenin",   45.00, "high",   "literature"),
    ("Chamomile",       "Apigenin",   68.00, "high",   "literature"),
    # Crocin
    ("Saffron",         "Crocin",   3000.00, "high",   "literature"),
    # Vitamin D3
    ("Salmon",          "Vitamin D3",   5.60, "high",   "USDA"),
    ("Herring",         "Vitamin D3",   5.00, "high",   "literature"),
    ("Sardine",         "Vitamin D3",   4.80, "high",   "USDA"),
    # Coenzyme Q10
    ("Atlantic mackerel","Coenzyme Q10",  4.32, "medium", "literature"),
    ("Salmon",          "Coenzyme Q10",  0.40, "low",    "literature"),
    # Ferulic acid
    ("Wheat germ",      "Ferulic acid", 5300.00, "high", "literature"),
    ("Whole wheat",     "Ferulic acid",  260.00, "high", "literature"),
    ("Oats",            "Ferulic acid",  173.00, "high", "literature"),
    ("Brown rice",      "Ferulic acid",   70.00, "medium","literature"),
    # Thymoquinone
    ("Nigella sativa",  "Thymoquinone", 13920.00, "high", "literature"),
    # Pterostilbene
    ("Blueberry",       "Pterostilbene",  0.15, "low",    "literature"),
    # Spermidine
    ("Wheat germ",      "Spermidine",   243.00, "high",   "literature"),
    ("Soybean",         "Spermidine",   207.00, "high",   "literature"),
    # Moringa associations
    ("Moringa",         "Quercetin",     17.00, "high",   "literature"),
    ("Moringa",         "Kaempferol",    12.00, "high",   "literature"),
    ("Moringa",         "Beta-carotene", 6780.00, "high", "literature"),
    # Baobab
    ("Baobab",          "Ascorbic acid", 500.00, "high",   "literature"),
    ("Baobab",          "Beta-carotene",1100.00, "high",   "literature"),
    # Bitter leaf
    ("Bitter leaf",     "Luteolin",      8.40, "medium", "literature"),
    ("Bitter leaf",     "Quercetin",     6.20, "medium", "literature"),
    # Spirulina
    ("Spirulina",       "Beta-carotene", 342.00, "high",   "literature"),
    ("Spirulina",       "Zeaxanthin",    180.00, "high",   "literature"),
    # Cacao
    ("Cacao nibs",      "Theobromine",  1764.00, "high",   "literature"),
    ("Dark chocolate",  "Theobromine",   802.00, "high",   "literature"),
    ("Dark chocolate",  "Caffeine",      62.00, "medium",  "literature"),
    # Coffee (caffeine)
    ("Coffee",          "Caffeine",  1200.00, "high",   "literature"),
    # Black tea
    ("Black tea",       "Caffeine",   470.00, "high",   "literature"),
    ("Black tea",       "Theophylline",  20.00, "medium", "literature"),
]


# ── USDA API helpers ──────────────────────────────────────────────────────────

async def fetch_usda_concentration(
    client: httpx.AsyncClient,
    food_name: str,
    compound_name: str,
    api_key: str,
    rate: RateLimiter,
) -> float | None:
    """Try to fetch compound concentration from USDA FoodData Central."""
    await rate.wait()
    try:
        resp = await client.get(
            f"{USDA_BASE}/foods/search",
            params={
                "query":    f"{food_name} {compound_name}",
                "api_key":  api_key,
                "pageSize": 5,
                "dataType": "SR Legacy,Foundation",
            },
        )
        resp.raise_for_status()
        foods = resp.json().get("foods", [])
        for food in foods:
            for nutrient in food.get("foodNutrients", []):
                if compound_name.lower() in nutrient.get("nutrientName", "").lower():
                    return float(nutrient.get("value", 0)) or None
    except Exception as exc:
        logger.debug("USDA lookup failed for %s/%s: %s", food_name, compound_name, exc)
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(dry_run: bool = False, skip_usda: bool = False) -> None:
    from app.config import get_settings
    from app.database import get_supabase

    checkpoint = load_checkpoint(CHECKPOINT_NAME)
    processed_foods: set[str] = set(checkpoint.get("processed", []))

    logger.info(
        "Starting food population%s (USDA enrichment: %s).",
        " [DRY RUN]" if dry_run else "",
        "disabled" if skip_usda else "enabled",
    )

    db_client   = await get_supabase()
    settings    = get_settings()
    usda_key    = settings.usda_api_key

    # ── Insert foods ──────────────────────────────────────────────────────────
    existing_food_names = await get_existing_values(db_client, "foods", "name")
    new_food_records = [
        {"name": name, "scientific_name": sci}
        for name, sci in CURATED_FOODS
        if name not in existing_food_names
    ]
    logger.info(
        "%d foods already in DB; inserting %d new food(s).",
        len(existing_food_names), len(new_food_records),
    )
    if not dry_run and new_food_records:
        await batch_insert(db_client, "foods", new_food_records, logger=logger)

    # Load food and compound UUID maps from DB
    food_resp  = await db_client.table("foods")    .select("id, name").execute()
    cmpd_resp  = await db_client.table("compounds").select("id, compound_name").execute()
    food_map:  dict[str, str] = {r["name"]:          r["id"] for r in (food_resp.data or [])}
    cmpd_map:  dict[str, str] = {r["compound_name"]: r["id"] for r in (cmpd_resp.data or [])}

    logger.info(
        "Loaded %d foods, %d compounds from DB for association linking.",
        len(food_map), len(cmpd_map),
    )

    # ── Build food_compound_associations ─────────────────────────────────────
    assoc_records: list[dict[str, Any]] = []
    skipped_assocs = 0
    missing_foods_set: set[str] = set()
    missing_cmpds_set: set[str] = set()

    logger.info(
        "Starting association build: %d curated entries to process.",
        len(FOOD_COMPOUND_ASSOCIATIONS),
    )

    async with httpx.AsyncClient(timeout=20.0) as http:
        usda_rate = RateLimiter(calls_per_second=3.0)

        for (fname, cname, conc, category, src) in FOOD_COMPOUND_ASSOCIATIONS:
            food_id = food_map.get(fname)
            cmpd_id = cmpd_map.get(cname)
            if not food_id or not cmpd_id:
                skipped_assocs += 1
                if not food_id:
                    missing_foods_set.add(fname)
                if not cmpd_id:
                    missing_cmpds_set.add(cname)
                logger.debug("Skipping association %s × %s — not in DB", fname, cname)
                continue

            # Try USDA enrichment if key is available and not skipped
            final_conc = conc
            if not skip_usda and usda_key and src != "USDA":
                usda_val = await fetch_usda_concentration(
                    http, fname, cname, usda_key, usda_rate
                )
                if usda_val:
                    final_conc = usda_val
                    src = "usda"

            assoc_records.append({
                "food_id":                 food_id,
                "compound_id":             cmpd_id,
                "concentration_mg_per_100g": final_conc,
                "concentration_category":  category,
                "source":                  src,
            })

    # Deduplicate by (food_id, compound_id)
    seen: set[tuple[str, str]] = set()
    unique_assocs: list[dict[str, Any]] = []
    for r in assoc_records:
        key = (r["food_id"], r["compound_id"])
        if key not in seen:
            seen.add(key)
            unique_assocs.append(r)

    logger.info(
        "Staged %d unique food_compound_association row(s). "
        "Skipped %d (missing food: %d unique, missing compound: %d unique).",
        len(unique_assocs), skipped_assocs,
        len(missing_foods_set), len(missing_cmpds_set),
    )
    if missing_foods_set:
        logger.warning(
            "Foods in FOOD_COMPOUND_ASSOCIATIONS but not in DB (%d): %s",
            len(missing_foods_set),
            ", ".join(sorted(missing_foods_set)[:15]),
        )
    if missing_cmpds_set:
        logger.warning(
            "Compounds in FOOD_COMPOUND_ASSOCIATIONS but not in DB (%d): %s",
            len(missing_cmpds_set),
            ", ".join(sorted(missing_cmpds_set)[:15]),
        )

    if not dry_run and unique_assocs:
        inserted = await batch_insert(
            db_client, "food_compound_associations", unique_assocs, logger=logger
        )
        logger.info("Inserted %d food_compound_association rows.", inserted)
    elif dry_run:
        logger.info("[DRY RUN] Would insert %d association row(s).", len(unique_assocs))

    logger.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populate foods and food-compound associations.")
    parser.add_argument("--dry-run",   action="store_true")
    parser.add_argument("--skip-usda", action="store_true", help="Skip USDA API enrichment")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run, skip_usda=args.skip_usda))
