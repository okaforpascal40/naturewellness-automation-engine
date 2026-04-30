-- Migration: create phytochemical_sources lookup table
-- Maps each phytochemical (from CTD whitelist) to the fruits/vegetables
-- that contain it, plus a chemical class for grouping.

CREATE TABLE IF NOT EXISTS phytochemical_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phytochemical_name TEXT UNIQUE NOT NULL,
    fruit_vegetables TEXT[] NOT NULL,
    primary_sources TEXT[],
    chemical_class TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_phytochemical_name
    ON phytochemical_sources(phytochemical_name);

-- Case-insensitive lookups (the CTD module returns names with mixed casing)
CREATE INDEX IF NOT EXISTS idx_phytochemical_name_lower
    ON phytochemical_sources(LOWER(phytochemical_name));

-- GIN index on the array columns for fast contains-queries
CREATE INDEX IF NOT EXISTS idx_phytochemical_fruit_vegetables
    ON phytochemical_sources USING GIN(fruit_vegetables);
