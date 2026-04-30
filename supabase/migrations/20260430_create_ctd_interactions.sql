-- Migration: ctd_phytochemical_gene_interactions
-- Pre-loaded snapshot of CTD's curated chemical-gene interactions, filtered
-- to phytochemicals (whitelist in app/api/ctd_api.py) and human (taxon 9606).
-- Source: https://ctdbase.org/reports/CTD_chem_gene_ixns.tsv.gz

CREATE TABLE IF NOT EXISTS ctd_phytochemical_gene_interactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chemical_name TEXT NOT NULL,
    chemical_id TEXT,
    gene_symbol TEXT NOT NULL,
    gene_id TEXT,
    interaction_actions TEXT[],
    pubmed_ids TEXT[],
    publication_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ctd_gene_symbol
    ON ctd_phytochemical_gene_interactions(gene_symbol);

CREATE INDEX IF NOT EXISTS idx_ctd_chemical_name
    ON ctd_phytochemical_gene_interactions(chemical_name);
