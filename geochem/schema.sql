-- GeoChem By-Product tab: Postgres index schema
-- Populated from the Fuseki SPARQL endpoint by build_geochem_index.py
--
-- Run with:
--   docker exec -i geochem-postgres psql -U geochem -d geochem < schema.sql

DROP TABLE IF EXISTS paper_commodities CASCADE;
DROP TABLE IF EXISTS measurements CASCADE;
DROP TABLE IF EXISTS samples CASCADE;
DROP TABLE IF EXISTS sites CASCADE;
DROP TABLE IF EXISTS papers CASCADE;


CREATE TABLE papers (
    paper_uri   TEXT PRIMARY KEY,
    title       TEXT,
    doi         TEXT,
    year        INTEGER,
    journal     TEXT,
    indexed_at  TIMESTAMPTZ DEFAULT now()   -- lets you see what the last build touched
);


CREATE TABLE sites (
    site_uri                 TEXT PRIMARY KEY,
    paper_uri                TEXT REFERENCES papers(paper_uri) ON DELETE CASCADE,
    name                     TEXT,          -- "deposit name" shown in the expanded table
    country                  TEXT,          -- sparse (~30% coverage)
    state                    TEXT,          -- state/province, same sparsity as country
    -- Informational only. NOT the source for the commodity filter -- that comes
    -- from measured Element labels. Kept for a future site detail view.
    primary_commodities      TEXT,
    secondary_commodities    TEXT,
    deposit_type_text        TEXT,
    deposit_type_confidence  REAL
);
CREATE INDEX idx_sites_paper   ON sites(paper_uri);
CREATE INDEX idx_sites_country ON sites(country);


CREATE TABLE samples (
    sample_uri       TEXT PRIMARY KEY,
    site_uri         TEXT REFERENCES sites(site_uri) ON DELETE CASCADE,
    sample_id        TEXT,   -- candidate for "sample number"
    sample_local_id  TEXT,   -- other candidate; confirm which one the team means
    sample_name      TEXT,
    sample_type      TEXT,
    mineral          TEXT    -- host mineral analyzed, e.g. "sphalerite"
);
CREATE INDEX idx_samples_site ON samples(site_uri);


CREATE TABLE measurements (
    id                 BIGSERIAL PRIMARY KEY,
    sample_uri         TEXT REFERENCES samples(sample_uri) ON DELETE CASCADE,
    analysis_uri       TEXT,
    analysis_id        TEXT,    -- short human analysis id, e.g. "LA1-1_1_AG"
    analytical_method  TEXT,
    element_label      TEXT,    -- from Element rdfs:label, e.g. "Ag"
    grade              DOUBLE PRECISION,
    grade_raw          TEXT,    -- original string, kept when it won't parse as a number
    grade_unit_uri     TEXT,
    detection_limit    DOUBLE PRECISION
);
CREATE INDEX idx_meas_sample  ON measurements(sample_uri);
CREATE INDEX idx_meas_element ON measurements(element_label);
-- Composite index: the expand-a-paper query filters on both at once
CREATE INDEX idx_meas_element_sample ON measurements(element_label, sample_uri);


-- Derived from measurements. This is the table the commodity filter hits.
CREATE TABLE paper_commodities (
    paper_uri        TEXT REFERENCES papers(paper_uri) ON DELETE CASCADE,
    commodity_label  TEXT,
    PRIMARY KEY (paper_uri, commodity_label)
);
CREATE INDEX idx_pc_commodity ON paper_commodities(commodity_label);
