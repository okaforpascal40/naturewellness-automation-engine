"""Step 3 — Populate the compounds table with curated natural food bioactives.

Uses PubChem PUG REST API to validate each compound and retrieve its CID
(PubChem ID) and canonical description, then inserts into Supabase.

Usage:
    python data_population/populate_compound_data.py [--dry-run] [--skip-pubchem]

Target: 100+ compound rows in the compounds table.
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

PUBCHEM_BASE    = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
CHECKPOINT_NAME = "compounds"

logger = setup_logging("compounds")

# ── Curated compound master list ──────────────────────────────────────────────
# Format: (compound_name, compound_type, fallback_description)
# PubChem enrichment will overwrite the description when available.

CURATED_COMPOUNDS: list[tuple[str, str, str]] = [
    # Flavonols
    ("Quercetin",      "flavonol",   "Plant flavonol with antioxidant, anti-inflammatory, and antiviral properties."),
    ("Kaempferol",     "flavonol",   "Natural flavonoid with anti-inflammatory, antioxidant, and anticancer activities."),
    ("Myricetin",      "flavonol",   "Natural flavonoid from fruits and herbs with potent antioxidant properties."),
    ("Fisetin",        "flavonol",   "Bioactive flavonoid with senolytic and neuroprotective properties."),
    ("Rutin",          "flavonol",   "Glycoside of quercetin with vascular-protective effects."),
    ("Isorhamnetin",   "flavonol",   "O-methylated metabolite of quercetin found in ginger and onion."),
    ("Morin",          "flavonol",   "Flavonoid found in white mulberry and guava with antioxidant effects."),
    # Flavones
    ("Luteolin",       "flavone",    "Naturally occurring flavone with anti-inflammatory effects."),
    ("Apigenin",       "flavone",    "Plant-derived flavone from chamomile and parsley with anxiolytic properties."),
    ("Baicalein",      "flavone",    "Flavone from Scutellaria baicalensis with anticancer activities."),
    ("Chrysin",        "flavone",    "Naturally occurring flavone found in honey and passion flower."),
    ("Acacetin",       "flavone",    "O-methylated flavone found in damiana and black locust with anti-inflammatory effects."),
    ("Nobiletin",      "flavone",    "Polymethoxylated flavone from citrus peel with neuroprotective properties."),
    ("Tangeretin",     "flavone",    "Polymethoxylated flavone from tangerine peel with anticancer activities."),
    # Flavanones
    ("Naringenin",     "flavanone",  "Citrus flavanone with antioxidant and anti-inflammatory properties."),
    ("Hesperetin",     "flavanone",  "Flavanone from citrus fruits with cardioprotective effects."),
    ("Naringin",       "flavanone",  "Predominant flavanone glycoside in grapefruit with lipid-lowering effects."),
    ("Hesperidin",     "flavanone",  "Flavanone glycoside in citrus peel with anti-inflammatory and antioxidant effects."),
    ("Eriodictyol",    "flavanone",  "Flavanone from lemon and yerba santa with antioxidant properties."),
    # Catechins
    ("Epigallocatechin gallate", "catechin", "Most abundant catechin in green tea with potent anticancer properties."),
    ("Epicatechin",    "catechin",   "Flavonoid in cocoa and tea with cardiovascular protective effects."),
    ("Catechin",       "catechin",   "Natural phenol and antioxidant found in tea, wine, and chocolate."),
    ("Epigallocatechin", "catechin", "Catechin in green tea with antioxidant properties."),
    ("Procyanidin B2", "proanthocyanidin", "Condensed tannin in apples and cocoa with cardiovascular protective effects."),
    # Isoflavones
    ("Genistein",      "isoflavone", "Phytoestrogen from soybeans with anticancer and cardioprotective effects."),
    ("Daidzein",       "isoflavone", "Soy isoflavone phytoestrogen with bone-protective benefits."),
    ("Formononetin",   "isoflavone", "O-methylated isoflavone from red clover with estrogenic activities."),
    ("Biochanin A",    "isoflavone", "O-methylated isoflavone from chickpeas with anticancer properties."),
    # Anthocyanins
    ("Cyanidin",       "anthocyanin", "Naturally occurring anthocyanin in red and purple fruits with antioxidant properties."),
    ("Delphinidin",    "anthocyanin", "Blue-red anthocyanin pigment in berries with antioxidant effects."),
    ("Malvidin",       "anthocyanin", "Blue anthocyanin abundant in red wine and blueberries."),
    ("Pelargonidin",   "anthocyanin", "Orange-red anthocyanin found in strawberries."),
    # Stilbenoids
    ("Resveratrol",    "stilbenoid", "Stilbenoid in red wine and grapes with cardioprotective and anti-aging effects."),
    ("Pterostilbene",  "stilbenoid", "Natural stilbenoid in blueberries with superior bioavailability vs resveratrol."),
    # Curcuminoids
    ("Curcumin",         "curcuminoid", "Primary curcuminoid in turmeric with potent anti-inflammatory and anticancer properties."),
    ("Demethoxycurcumin","curcuminoid", "Natural curcuminoid from turmeric with anti-inflammatory activities."),
    # Phenolic & hydroxycinnamic acids
    ("Ellagic acid",    "polyphenol",           "Polyphenol in pomegranates and berries with antiproliferative effects."),
    ("Gallic acid",     "phenolic acid",         "Trihydroxybenzoic acid in tea and nuts with antioxidant properties."),
    ("Caffeic acid",    "hydroxycinnamic acid",  "Hydroxycinnamic acid in coffee and fruits with anti-inflammatory effects."),
    ("Ferulic acid",    "hydroxycinnamic acid",  "Ubiquitous plant constituent in grains with antioxidant properties."),
    ("Chlorogenic acid","hydroxycinnamic acid",  "Major phenolic compound in coffee linked to cardiovascular health."),
    ("Rosmarinic acid", "hydroxycinnamic acid",  "Phenolic compound in rosemary and Lamiaceae herbs."),
    ("p-Coumaric acid", "hydroxycinnamic acid",  "Hydroxycinnamic acid found in peanuts and tomatoes."),
    ("Sinapic acid",    "hydroxycinnamic acid",  "Phenolic acid found in brassica vegetables and mustard."),
    ("Protocatechuic acid", "phenolic acid",     "Dihydroxybenzoic acid found in many plants with antioxidant activities."),
    ("Syringic acid",   "phenolic acid",         "Phenolic acid found in wine and fruits with antioxidant effects."),
    ("Hydroxytyrosol",  "phenolic",              "Principal phenol in olive oil with potent antioxidant activity."),
    ("Oleuropein",      "secoiridoid",           "Bitter compound in olive with cardioprotective and antimicrobial properties."),
    ("Tyrosol",         "phenolic",              "Phenolic compound in olive oil and wine with cardioprotective effects."),
    ("Vanillin",        "phenolic aldehyde",     "Primary component of vanilla bean extract with antioxidant properties."),
    # Carotenoids
    ("Beta-carotene",    "carotenoid", "Provitamin A carotenoid in carrots and sweet potatoes."),
    ("Lycopene",         "carotenoid", "Bright red carotenoid in tomatoes associated with reduced cancer risk."),
    ("Lutein",           "carotenoid", "Carotenoid in spinach and kale protecting against macular degeneration."),
    ("Zeaxanthin",       "carotenoid", "Carotenoid pigment in corn and saffron protecting eye health."),
    ("Astaxanthin",      "carotenoid", "Red carotenoid in microalgae and seafood with potent antioxidant effects."),
    ("Alpha-carotene",   "carotenoid", "Provitamin A carotenoid in carrots and pumpkin."),
    ("Beta-cryptoxanthin","carotenoid","Carotenoid in oranges and papayas with provitamin A activity."),
    ("Fucoxanthin",      "carotenoid", "Marine carotenoid in brown seaweed with anti-obesity activities."),
    ("Crocin",           "carotenoid", "Water-soluble carotenoid from saffron with antidepressant and neuroprotective effects."),
    ("Crocetin",         "carotenoid", "Apocarotenoid from saffron with cardioprotective and anti-inflammatory properties."),
    # Organosulfur
    ("Allicin",          "organosulfur",  "Primary bioactive in garlic with antimicrobial and cardiovascular effects."),
    ("Sulforaphane",     "isothiocyanate","Isothiocyanate from broccoli that activates Nrf2 pathway."),
    ("Diallyl disulfide","organosulfur",  "Organosulfur compound from garlic with anticancer effects."),
    ("Indole-3-carbinol","indole",        "Phytochemical from cruciferous vegetables modulating estrogen metabolism."),
    ("Diindolylmethane", "indole",        "Compound from broccoli with anticancer and immunomodulatory effects."),
    # Omega fatty acids
    ("Docosahexaenoic acid",  "omega-3 fatty acid", "Omega-3 fatty acid in oily fish essential for brain development."),
    ("Eicosapentaenoic acid", "omega-3 fatty acid", "Omega-3 fatty acid with anti-inflammatory and cardiovascular effects."),
    ("Alpha-linolenic acid",  "omega-3 fatty acid", "Essential omega-3 in flaxseeds, walnuts, and chia seeds."),
    ("Gamma-linolenic acid",  "omega-6 fatty acid", "Omega-6 in evening primrose oil with anti-inflammatory properties."),
    ("Conjugated linoleic acid","fatty acid",       "Naturally occurring fatty acid in meat and dairy with anticancer effects."),
    ("Oleic acid",            "monounsaturated fatty acid", "Predominant fatty acid in olive oil linked to cardiovascular health."),
    # Terpenoids / phenylpropanoids
    ("Gingerol",         "phenylalkanoid", "Main bioactive in fresh ginger with anti-nausea and antioxidant effects."),
    ("Capsaicin",        "capsaicinoid",   "Active component in chili peppers activating TRPV1 receptors."),
    ("Piperine",         "alkaloid",       "Alkaloid from black pepper enhancing bioavailability of other compounds."),
    ("Thymoquinone",     "terpenoid",      "Major bioactive of Nigella sativa (black seed) with anticancer properties."),
    ("Carvacrol",        "terpenoid",      "Monoterpenoid phenol in oregano and thyme with antimicrobial effects."),
    ("Thymol",           "terpenoid",      "Natural monoterpene in thyme and oregano with antiseptic properties."),
    ("Limonene",         "terpenoid",      "Cyclic monoterpene in citrus peel with anticancer and anti-anxiety effects."),
    ("Ursolic acid",     "triterpenoid",   "Pentacyclic triterpenoid in apple peel and rosemary with anticancer effects."),
    ("Oleanolic acid",   "triterpenoid",   "Triterpenoid in olive oil with hepatoprotective and anti-inflammatory effects."),
    ("Betulinic acid",   "triterpenoid",   "Lupane triterpenoid in birch bark with anticancer properties."),
    ("Andrographolide",  "diterpenoid",    "Principal bioactive of Andrographis with anti-inflammatory effects."),
    # Alkaloids
    ("Berberine",        "alkaloid", "Plant alkaloid in barberry with antidiabetic and anticancer effects."),
    ("Caffeine",         "alkaloid", "Methylxanthine in coffee, tea, and cocoa stimulating the CNS."),
    ("Theophylline",     "alkaloid", "Methylxanthine in tea with bronchodilatory effects."),
    ("Theobromine",      "alkaloid", "Methylxanthine in cocoa with cardiovascular and neuroprotective effects."),
    # Vitamins as bioactives
    ("Vitamin D3",       "secosteroid", "Secosteroid hormone produced in skin on sunlight exposure."),
    ("Alpha-tocopherol", "tocopherol",  "Most bioactive form of vitamin E with antioxidant properties."),
    ("Ascorbic acid",    "vitamin",     "Vitamin C — essential water-soluble antioxidant in citrus and vegetables."),
    # Coenzymes / other bioactives
    ("Coenzyme Q10",     "quinone",       "Ubiquitous antioxidant in cellular energy production."),
    ("Alpha-lipoic acid","organosulfur",  "Universal antioxidant in spinach and yeast with anti-inflammatory effects."),
    ("Melatonin",        "tryptamine",    "Hormone in cherries and walnuts regulating circadian rhythm."),
    ("Spermidine",       "polyamine",     "Naturally occurring polyamine in wheat germ and mushrooms with autophagy-inducing effects."),
    ("Ergothioneine",    "amino acid",    "Naturally occurring antioxidant amino acid found mainly in mushrooms."),
    # Lignans
    ("Secoisolariciresinol","lignan",     "Plant lignan in flaxseeds with phytoestrogenic effects."),
    ("Matairesinol",        "lignan",     "Lignan in sesame seeds and whole grains converted to enterolactone."),
    # Flavonolignans / other phenolics
    ("Silymarin",        "flavonolignan", "Polyphenolic complex from milk thistle with hepatoprotective effects."),
    ("Honokiol",         "neolignan",     "Bioactive from magnolia bark with anxiolytic and anticancer properties."),
    ("Urolithin A",      "polyphenol",    "Gut microbiome metabolite of ellagitannins with mitophagy-inducing effects."),
    ("Taxifolin",        "dihydroflavonol","Dihydroquercetin found in Siberian larch with antioxidant properties."),
]


# ── PubChem helpers ───────────────────────────────────────────────────────────

async def fetch_pubchem_cid(
    client: httpx.AsyncClient,
    compound_name: str,
    rate: RateLimiter,
) -> str | None:
    """Return the PubChem CID string for a compound name, or None if not found."""
    await rate.wait()
    url = f"{PUBCHEM_BASE}/compound/name/{compound_name.replace(' ', '%20')}/cids/JSON"
    try:
        resp = await client.get(url)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        cids: list[int] = resp.json().get("IdentifierList", {}).get("CID", [])
        return str(cids[0]) if cids else None
    except Exception as exc:
        logger.warning("PubChem CID lookup failed for '%s': %s", compound_name, exc)
        return None


async def fetch_pubchem_description(
    client: httpx.AsyncClient,
    cid: str,
    rate: RateLimiter,
) -> str:
    """Return the first paragraph of the PubChem compound description."""
    await rate.wait()
    url = f"{PUBCHEM_BASE}/compound/cid/{cid}/description/JSON"
    try:
        resp = await client.get(url)
        if resp.status_code == 404:
            return ""
        resp.raise_for_status()
        infos: list[dict] = resp.json().get("InformationList", {}).get("Information", [])
        for info in infos:
            desc = info.get("Description", "")
            if desc and len(desc) > 20:
                return desc[:500]     # cap at 500 chars
    except Exception as exc:
        logger.debug("PubChem description lookup failed for CID %s: %s", cid, exc)
    return ""


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(dry_run: bool = False, skip_pubchem: bool = False) -> None:
    from app.database import get_supabase

    checkpoint = load_checkpoint(CHECKPOINT_NAME)
    processed: set[str] = set(checkpoint.get("processed", []))

    logger.info(
        "Starting compound population%s%s. Already processed: %d.",
        " [DRY RUN]" if dry_run else "",
        " [SKIP PUBCHEM]" if skip_pubchem else "",
        len(processed),
    )

    db_records: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=20.0) as client:
        # PubChem free tier: 5 requests/second
        rate = RateLimiter(calls_per_second=4.0)

        for name, ctype, fallback_desc in CURATED_COMPOUNDS:
            if name in processed:
                continue

            if skip_pubchem:
                cid = None
                description = fallback_desc
                logger.debug("PubChem skipped for '%s' — using fallback description", name)
            else:
                cid = await fetch_pubchem_cid(client, name, rate)
                description = fallback_desc

                if cid:
                    enriched = await fetch_pubchem_description(client, cid, rate)
                    if enriched:
                        description = enriched
                    logger.debug("PubChem: %s → CID %s", name, cid)
                else:
                    logger.debug("PubChem: no CID for '%s' — using fallback description", name)

            db_records.append({
                "compound_name": name,
                "compound_type": ctype,
                "description":   description,
                "pubchem_id":    cid or "",
            })
            checkpoint["processed"].append(name)
            save_checkpoint(CHECKPOINT_NAME, checkpoint)

    logger.info("Collected %d compound record(s).", len(db_records))

    if not dry_run:
        db_client = await get_supabase()
        existing = await get_existing_values(db_client, "compounds", "compound_name")
        new_records = [r for r in db_records if r["compound_name"] not in existing]
        logger.info(
            "%d already in DB; inserting %d new compound(s).",
            len(existing),
            len(new_records),
        )
        inserted = await batch_insert(db_client, "compounds", new_records, logger=logger)
        logger.info("Inserted %d compound rows.", inserted)
    else:
        logger.info("[DRY RUN] Would insert %d compound row(s).", len(db_records))

    logger.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populate compounds table.")
    parser.add_argument("--dry-run",       action="store_true")
    parser.add_argument("--skip-pubchem",  action="store_true",
                        help="Skip PubChem validation (10x faster; uses fallback descriptions)")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run, skip_pubchem=args.skip_pubchem))
