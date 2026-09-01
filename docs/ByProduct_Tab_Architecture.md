# By-Product Tab: Data Architecture & Implementation Plan

**Author:** RK
**Status:** Draft for team review
**Scope:** How the By-Product dashboard tab will source, index, and serve GeoChem trace-element data

---

## TL;DR

The GeoChem data (element/grade measurements extracted from journal articles) lives in an RDF triple store, queried via SPARQL. We will **not** query SPARQL live every time a user interacts with the dashboard. Instead, a background job periodically extracts the GeoChem data into a **Postgres index** — a small set of plain relational tables — and the dashboard reads only from Postgres. This keeps the tab fast and predictable regardless of how large the underlying RDF store grows (79 papers today, expected to reach 10,000s).

The one non-obvious design decision worth flagging up front: a paper's "commodities" for filtering purposes are derived **entirely from measured `geochem:Element` data**, not from the site's curated `primary_commodities`/`secondary_commodities` text fields — see [The Commodity Definition Problem](#4-the-commodity-definition-problem) for why.

---

## 1. What We're Building

A new dashboard tab showing byproduct trace-element data (e.g. gallium, germanium, indium) sourced from peer-reviewed journal articles, alongside MinMod's existing mineral-site data (sourced mainly from company mining reports).

**UI shape**, matching the existing MinMod site-data tab:

- Filters at the top: **Commodity**, **Country** (Deposit Type is deferred — see [§9](#9-open-questions--known-limitations))
- Selecting a commodity displays a table of every paper that has that commodity measured
- Clicking a paper expands it into one row per sample, showing: **deposit name** (the site's name), **sample number**, **sample name**, and **grade** — specifically, the grade of the *selected* commodity in that sample, not every element measured

---

## 2. Where the Data Lives

GeoChem paper extractions are loaded as RDF triples into an Apache Jena Fuseki triple store, queried via SPARQL:

```
http://dev.minmod.isi.edu:3030/minmod/sparql
```

This is a **separate load from the production MinMod KG** — it currently contains only GeoChem-derived data (papers, sites, samples, analyses, elements), not the full MinMod mineral-site catalog. Current scale, as of last census:

| Class | Count |
|---|---|
| Papers (`MineralResourcePaper`) | 79 |
| Sites (`MineralSite`) | 421 |
| Samples | ~34,000 |
| Analyses | ~55,500 |
| Element measurements | ~822,000 |
| **Total triples** | **~5.16M** |

This is expected to grow to **10,000s of papers**. The architecture below is designed around that target, not the current 79.

The underlying data model, for reference:

```
MineralResourcePaper --has_mineral_site--> MineralSite --has_sample--> Sample
                                                                          |
                                                                    has_analysis
                                                                          v
                                                                      Analysis --element--> Element (grade, unit)
```

`MineralSite` also directly carries `primary_commodities`, `secondary_commodities` (comma-separated text fields), a `deposit_type_candidate`, and (sparsely) `country`.

---

## 3. The Core Design Decision: Precompute, Don't Query Live

**Why not just run a SPARQL query every time a user clicks a filter?**

Two reasons:

1. **Combined filters need composability.** The tab needs Commodity AND Country simultaneously. A relational database's `WHERE` clause does this natively; assembling the equivalent from raw SPARQL results on every request is slower and harder to maintain.
2. **Some of the underlying data (element measurements) is large and will grow.** At 10,000 papers we expect on the order of 100M `Element` nodes in the triple store. Querying across all of that live, on every filter click, does not scale — but it doesn't need to, because filter values themselves (which commodities exist, which papers have them) barely change once computed.

**The fix:** run the expensive SPARQL extraction **once**, in the background, and store the result in Postgres — the same relational database MinMod's existing dashboard already uses (`kgrel`) for its own mineral-site data. The dashboard then only ever talks to Postgres.

```
┌─────────────────────────────┐
│  Fuseki (dev.minmod.isi.edu) │   GeoChem RDF triples
│  ~5M triples today →         │   79 papers today → 10,000s later
│  10,000s of papers later     │
└──────────────┬───────────────┘
               │  batched, bounded SPARQL queries
               │  (only new papers each run — incremental)
               ▼
┌──────────────────────────────┐
│  Build script (Python/ETL)    │
└──────────────┬───────────────┘
               │  plain SQL INSERT/UPSERT
               ▼
┌──────────────────────────────┐
│  Postgres index               │   papers, sites, samples,
│  (small, fully indexed)       │   measurements, paper_commodities
└──────────────┬───────────────┘
               │  indexed SQL SELECT — milliseconds
               ▼
┌──────────────────────────────┐
│  Dash app — By-Product tab    │   Commodity + Country filters
│                                │   → paper list → expand → samples
└────────────────────────────────┘
```

This mirrors how MinMod already solves the identical problem for its main site data — the same RDF-and-Postgres dual-representation pattern, applied to GeoChem.

---

## 4. The Commodity Definition Problem

This is the most important modeling decision in the whole design, flagged directly by Prof. Knoblock: **any commodity that appears anywhere in a paper should be reflected in the filter — not just the ones explicitly labeled as primary or secondary.**

Two candidate sources exist in the data:

| Source | Where it lives | Has a grade attached? |
|---|---|---|
| Primary / secondary commodities | `MineralSite.primary_commodities` / `.secondary_commodities` (curated text fields, e.g. `"Sn, Pb, Zn, Cu"`) | **No** — plain text, no measurement |
| Measured elements | Every `Element` reachable via `Paper → Site → Sample → Analysis → Element`, e.g. `{"@type": "geochem:Element", "rdfs:label": "Ag"}` | **Yes** — each `Element` carries its own `grade` |

**Decision: use measured elements as the single source of truth for the commodity filter.** Two reasons this is the right call, not just a simplification for convenience:

1. It directly satisfies the professor's requirement — elements like arsenic that show up in a raw multi-element lab panel but never get called out as a "primary" or "secondary" commodity are exactly the by-products this tab exists to surface.
2. It avoids a real inconsistency the three-source version had: a paper could be listed under a commodity via `primary_commodities` text and then have **nothing to actually show** when the user expands it, since that field has no grade attached. Sourcing the filter from `Element` guarantees every paper listed under a commodity has real measurements to display.

`primary_commodities`/`secondary_commodities` are still stored on `sites` as informational context (useful for a site detail view later), but they are **not** used to determine which papers appear under a commodity filter.

**The index:**

```sql
CREATE TABLE paper_commodities (
    paper_uri        TEXT REFERENCES papers(paper_uri),
    commodity_label  TEXT   -- from Element.rdfs:label, e.g. "Ag"
);
CREATE UNIQUE INDEX uq_paper_commodity ON paper_commodities(paper_uri, commodity_label);
```

**Efficiency note:** this doesn't require a separate expensive scan of the ~822K `Element` nodes. The `measurements` table (§5) already extracts every element/grade value regardless — that's what populates the expanded per-sample rows the tab needs anyway. Once `measurements` is loaded, `paper_commodities` is just `SELECT DISTINCT paper_uri, element_label FROM measurements JOIN ...` — a cheap local SQL query, no extra SPARQL round trip.

---

## 5. Schema

Six tables, in Postgres (chosen to match the tech stack MinMod's existing dashboard already uses).

```sql
CREATE TABLE papers (
    paper_uri   TEXT PRIMARY KEY,
    title       TEXT,
    doi         TEXT,
    year        INTEGER,
    journal     TEXT
);

CREATE TABLE sites (
    -- primary_commodities / secondary_commodities below are informational only —
    -- NOT the source for the commodity filter (see §4)
    site_uri                 TEXT PRIMARY KEY,
    paper_uri                TEXT REFERENCES papers(paper_uri),
    name                     TEXT,
    primary_commodities      TEXT[],   -- parsed from "Sn, Pb, Zn, Cu"
    secondary_commodities    TEXT[],
    country                  TEXT,     -- sparse: ~30% coverage currently
    deposit_type_text        TEXT,     -- raw compound text, see §9
    deposit_type_confidence  REAL,
    deposit_type_source      TEXT
);
CREATE INDEX idx_primary_commodities   ON sites USING GIN (primary_commodities);
CREATE INDEX idx_secondary_commodities ON sites USING GIN (secondary_commodities);

CREATE TABLE site_commodity_candidates (   -- granular per-element rows, for cross-checking
    site_uri        TEXT REFERENCES sites(site_uri),
    candidate_uri   TEXT,
    observed_name   TEXT,
    normalized_uri  TEXT,
    confidence      REAL
);

CREATE TABLE samples (
    sample_uri     TEXT PRIMARY KEY,
    site_uri       TEXT REFERENCES sites(site_uri),
    sample_id      TEXT,   -- "sample number" shown in the expanded table — verify against sample_local_id, see §9
    sample_name    TEXT,
    sample_type    TEXT
);

CREATE TABLE measurements (
    sample_uri        TEXT REFERENCES samples(sample_uri),
    analysis_uri       TEXT,
    analytical_method  TEXT,
    element_label       TEXT,   -- from Element.rdfs:label, e.g. "Ag"
    grade               REAL,
    grade_unit_uri      TEXT,   -- raw URI kept as-is; see §9 (unit resolution gap)
    detection_limit     REAL
);
CREATE INDEX idx_element ON measurements(element_label);

-- Derived table, see §4 — built from measurements, not from primary/secondary commodities
CREATE TABLE paper_commodities (
    paper_uri        TEXT REFERENCES papers(paper_uri),
    commodity_label  TEXT
);
CREATE UNIQUE INDEX uq_paper_commodity ON paper_commodities(paper_uri, commodity_label);
```

---

## 6. Build Process (the "index," concretely)

A standalone Python script — **not part of the Dash app's request path** — run nightly or on-demand after new papers are uploaded to dev.

**Step 1 — find what's new (cheap, always safe to run):**

```python
all_papers = query("SELECT ?paper WHERE { ?paper a gc:MineralResourcePaper }")
already_indexed = {row[0] for row in fetch("SELECT paper_uri FROM papers")}
new_papers = [p for p in all_papers if p not in already_indexed]
```

**Step 2 — extract only the delta, in bounded batches:**

The key rule: **never run an unbounded query across the whole store.** At 79 papers this doesn't matter much; at 10,000s it's the difference between a query that finishes in seconds and one that risks timing out or exhausting memory on a shared server. Every extraction query is scoped to a batch of paper URIs via SPARQL's `VALUES` clause:

```sparql
PREFIX gc: <https://geochemistry.isi.edu/ontology/>
SELECT DISTINCT ?paper ?sample ?sampleName ?analysis ?method ?elementLabel ?grade ?gradeUnit
WHERE {
  VALUES ?paper { <paper_uri_1> <paper_uri_2> ... }   -- ~100 papers per batch
  ?paper gc:has_mineral_site/gc:has_sample ?sample .
  OPTIONAL { ?sample gc:sample_name ?sampleName }
  ?sample gc:has_analysis ?analysis .
  OPTIONAL { ?analysis gc:analytical_method ?method }
  ?analysis gc:element ?element .
  ?element rdfs:label ?elementLabel .
  OPTIONAL { ?element gc:grade ?grade }
  OPTIONAL { ?element gc:grade_unit ?gradeUnit }
}
```

**Step 3 — load into Postgres**, then derive `paper_commodities` with a single local SQL query against `measurements` (no further SPARQL needed — see §4):

```sql
INSERT INTO paper_commodities (paper_uri, commodity_label)
SELECT DISTINCT s.paper_uri, m.element_label
FROM measurements m
JOIN samples smp ON smp.sample_uri = m.sample_uri
JOIN sites s      ON s.site_uri = smp.site_uri
ON CONFLICT DO NOTHING;
```

This runs incrementally: each execution only processes papers added since the last run, so cost scales with **how much is new**, not with total store size.

---

## 7. Runtime Query Flow

Everything the dashboard does at request time is plain, indexed SQL — no SPARQL, no network call to Fuseki.

**Filter → paper list** (commodity selection displayed as a table of matching papers):

```sql
SELECT DISTINCT p.paper_uri, p.title, p.doi, p.year
FROM papers p
JOIN paper_commodities pc ON pc.paper_uri = p.paper_uri
LEFT JOIN sites s          ON s.paper_uri = p.paper_uri
WHERE pc.commodity_label = 'Zn'
  AND (s.country = 'China' OR :country_filter IS NULL)
```

**Click a paper → expand to sample rows.** This is filtered to the *selected* commodity — the expanded table shows that commodity's grade per sample, not every element measured in that sample:

```sql
SELECT s.name        AS deposit_name,      -- the site's name, e.g. "Suttsu"
       smp.sample_id  AS sample_number,
       smp.sample_name,
       m.grade,
       m.grade_unit_uri
FROM sites s
JOIN samples smp     ON smp.site_uri = s.site_uri
JOIN measurements m  ON m.sample_uri = smp.sample_uri
WHERE s.paper_uri = :paper_uri
  AND m.element_label = :selected_commodity   -- e.g. 'Zn', carried over from the filter click
```

Both run in milliseconds against an indexed table, regardless of RDF store size. Note "deposit name" here means the **site's name**, not the deposit *type* (`deposit_type_text`) — the two are easy to conflate and worth being explicit about when presenting.

---

## 8. Why This Scales to 10,000s of Papers

Two separate questions, worth keeping distinct when discussing this with the team:

- **Does the dashboard stay fast as the RDF store grows?** Yes — the dashboard never touches Fuseki. Query time depends only on the size of the Postgres tables, not the triple store.
- **Does the *build* step stay tractable?** Yes, for two reasons: (1) it's incremental — only new papers are processed each run; (2) every extraction query is batch-bounded via `VALUES`, so per-query cost stays constant regardless of total store size.

The table the dashboard actually filters on — `paper_commodities` — stays small even at scale: it's bounded by (papers × distinct element symbols per paper), not by raw element-measurement count. With a few dozen realistic element symbols across geochemistry papers, even 10,000 papers means on the order of a few hundred thousand rows — trivial for Postgres with a standard index.

---

## 9. Open Questions & Known Limitations

Worth raising with the team explicitly, not silently working around:

- **Deposit type is deferred from the filter for now.** `deposit_type_candidate.observed_name` is often a compound, comma-separated list of competing hypotheses (e.g. *"MVT zinc-lead, Sedimentary Exhalative zinc-lead, Skarn zinc-lead-silver"*) rather than a single clean label, generated via LLM classification with one confidence score covering multiple hypotheses at once. We have not yet confirmed whether `normalized_uri` (a clean resolvable entity) is populated — needs verification before deposit type can be a real filter.
- **Duplicate/identical deposit-type classifications across sites.** Several distinct sites within the same paper show byte-for-byte identical `observed_name`, `comments`, and `confidence` — suggesting classification happened once per paper/district rather than independently per site. Worth confirming this is intentional before presenting per-site confidence as independently assessed.
- **`sample_id` vs. `sample_local_id` for "sample number."** The `Sample` class has both — need to confirm which one matches what the professor means by "sample number" (likely the identifier as it appeared in the original paper) before wiring up the expanded table's column.
- **`primary_commodities`/`secondary_commodities` are no longer load-bearing for the filter**, but are still stored as informational fields on `sites`. Note for later: at least one site had two different `secondary_commodities` strings attached in the raw data (differing by one element) — worth deduping if this field is ever surfaced in the UI.
- **Country coverage is sparse** (~30% of sites currently). The Country filter will work but will under-cover a majority of papers until this improves upstream.
- **`grade_unit` does not currently resolve to a readable unit label** from this data store — units may need to be resolved via the production MinMod REST API (`/api/v1/units`) as a fallback, or units may need to be added to this store directly.
- **No confirmed join key back to production MinMod site records yet.** An earlier hypothesis (`mo:uri`) turned out to be self-referential, not a cross-reference. This affects whether GeoChem sites can ever be linked to full MinMod site records (commodity/country/deposit-type data from the main KG) — worth a direct question to the team.
- **Where should this Postgres database actually live?** Two options: extend MinMod's existing `kgrel` Postgres instance (cleaner long-term, and a real step toward a proper `kgrel.data.geochem` ETL pipeline), or run a standalone instance for now and migrate later. Recommend raising this directly rather than deciding unilaterally.

---

## 10. Next Steps

1. Confirm `deposit_type_candidate.normalized_uri` coverage — determines whether deposit type can be reintroduced as a real filter.
2. Confirm `sample_id` vs. `sample_local_id` — which one is the "sample number" to display.
3. Decide on the Postgres hosting question (§9) with the team.
4. Build and run the extraction script against all 79 current papers as a first end-to-end validation.
5. Wire the Dash filter UI to the Postgres queries in §7.
