# By-Product Tab: Data Architecture & Implementation Plan

**Author:** RK
**Status:** Implemented on `feature/byproduct_sparql`
**Scope:** How the By-Product dashboard tab sources and serves GeoChem trace-element data

---

## TL;DR

The GeoChem data (element/grade measurements extracted from journal articles) lives in an RDF triple store. The dashboard **queries that store directly over SPARQL** on each interaction. There is no intermediate database.

An earlier revision of this document argued the opposite — that live SPARQL would be too slow, and that a Postgres index was required. That design was built and then measured against the real store. The measurements did not support it: four of the five queries the tab needs are either pinned to a single subtree or scale with the *selected commodity's* slice of the store rather than the store total. [§3](#3-the-core-design-decision-query-live-with-the-right-query-shapes) records the numbers, because the reasoning matters more than the conclusion — it tells us exactly which query would force a rethink, and at what point.

The one non-obvious modeling decision, unchanged from the earlier revision: a paper's "commodities" for filtering purposes are derived **entirely from measured `geochem:Element` data**, not from the site's curated `primary_commodities`/`secondary_commodities` text fields — see [The Commodity Definition Problem](#4-the-commodity-definition-problem).

---

## 1. What We're Building

A dashboard tab showing byproduct trace-element data (e.g. gallium, germanium, indium) sourced from peer-reviewed journal articles, alongside MinMod's existing mineral-site data (sourced mainly from company mining reports).

**UI shape**, matching the existing MinMod site-data tab — a three-level drill-down:

- Filters at the top: **Commodity**, **Country** (Deposit Type is deferred — see [§9](#9-open-questions--known-limitations))
- Selecting a commodity lists every **paper** that measured it — title, journal, year, DOI
- Clicking a paper lists every **deposit site** in that paper that measured it — deposit name, country, state, deposit type, confidence
- Clicking a deposit lists every **sample measurement** — sample ID, sample name, mineral, analysis ID, grade, method

Each level shows only data for the *selected* commodity, carried down from the filter.

---

## 2. Where the Data Lives

GeoChem paper extractions are loaded as RDF triples into an Apache Jena Fuseki triple store:

```
http://dev.minmod.isi.edu:3030/minmod/sparql        (ISI VPN required)
```

This is a **separate load from the production MinMod KG** — it contains only GeoChem-derived data (papers, sites, samples, analyses, elements), not the full MinMod mineral-site catalog. Current scale:

| Class | Count |
|---|---|
| Papers (`MineralResourcePaper`) | 97 |
| Sites (`MineralSite`) | ~421 |
| Samples | ~34,000 |
| Analyses | ~55,500 |
| Element measurements | ~822,000 |
| Distinct element labels | 68 |
| **Total triples** | **~5.16M** |

Expected to grow to **10,000s of papers**.

```
MineralResourcePaper --has_mineral_site--> MineralSite --has_sample--> Sample
                                                                          |
                                                                    has_analysis
                                                                          v
                                                                      Analysis --element--> Element (grade, unit)
```

`MineralSite` also carries `primary_commodities`, `secondary_commodities` (comma-separated text), a `deposit_type_candidate`, and (sparsely) `country`.

Three properties of this store are easy to get wrong and are worth stating explicitly, because each one silently produces wrong output rather than an error:

- **The site display name is `rdfs:label`, not `mo:name`.** `mo:name` does not exist on Site nodes; querying it yields empty deposit names.
- **`country` and `state_or_province` are not literals.** They point at a CandidateEntity whose readable value is one hop further on, via `mo:observed_name`.
- **`gc:paper_year` has zero triples in the store.** The value exists in the upstream extraction but is dropped during ingestion, so the Year column is blank for every paper. The UI reports the year from that predicate only, and will populate on its own once ingestion is fixed; deriving it from `gc:paper_id` or from the paper URI slug was tried and deliberately reverted, since neither is the year as recorded in the data.

---

## 3. The Core Design Decision: Query Live, With the Right Query Shapes

The earlier revision of this doc asserted that live SPARQL could not scale and that an index was required. Having built both, here are the measured numbers against the real store (97 papers, 822K elements):

| Query | Cost scales with | Measured |
|---|---|---|
| Commodity dropdown | **total elements in the store** | **2.7s** |
| Countries for a commodity | that commodity's elements | 0.05s (Br) – 0.45s (Ga) |
| Papers for a commodity | that commodity's elements | 0.06s (Br) – 0.60s (Fe) |
| Deposits in a paper | one paper's subtree | 0.05 – 0.31s |
| Samples in a deposit | one site's subtree | 0.06 – 0.48s (3,921 rows) |

Three conclusions, which are what actually drive the design:

**The two drill-down queries are fine live, permanently.** `VALUES` pins them to a single paper or site. A paper does not get bigger because the store got bigger. The 0.48s worst case is already the heaviest site in the store (Mount Isa, 3,921 Ga measurements) and is dominated by transferring ~2.4 MB of JSON, not by matching triples.

**The commodity-filtered queries scale with the selection, not the store** — but only because of how they are written. See below.

**One query is a genuine full scan**, and it is the commodity dropdown, which cannot be selective: you cannot know the distinct set of element labels without visiting every label. This is the canary. It is cached (1 hour), and given a deliberately short 90s timeout so that if it ever starts timing out, that is an unmissable signal the store has outgrown live querying — rather than the tab quietly taking three minutes to load.

### The query shape that makes this work

Every commodity-filtered query starts from the **element label and walks up** to the site or paper, wrapped in an inner `SELECT DISTINCT`:

```sparql
SELECT DISTINCT ?paper WHERE {
  ?element rdfs:label "Ga" .
  ?paper gc:has_mineral_site/gc:has_sample/gc:has_analysis/gc:element ?element .
}
```

The obvious alternative — start from papers, filter with `FILTER EXISTS` — is dramatically worse for exactly the commodities this tab exists to serve:

| Commodity | Elements | Walk-up (used) | `FILTER EXISTS` |
|---|---|---|---|
| Zn (common) | 31,798 | 0.33s | 0.25s |
| Br (rare) | 32 | **0.05s** | 1.05s |

`FILTER EXISTS` has to prove *absence*, which forces a full enumeration of every element under every non-matching paper — so its cost tracks the store total no matter how narrow the selection. The walk-up form's cost tracks the selected commodity's own element count. Byproducts like Ga, Ge and In are rare by definition, so the tab's primary use case is precisely where the difference is largest.

Two supporting details in the same spirit: the inner `SELECT DISTINCT` collapses ~32K element rows down to a few hundred papers *before* any `OPTIONAL` metadata runs (0.33s vs 0.45s), so all `OPTIONAL`s are kept outside the subselect.

### Why not an index

The index was a real cost, not a free safety net: a second service to run, a schema to migrate, an ETL script to re-run, and a staleness window in which the dashboard showed something other than what the triple store held. Removing it means `docker compose up` starts one service, and newly ingested papers appear immediately with no re-index step.

The trade accepted in exchange: **the ISI VPN is now required at request time, not just at build time.** The tab degrades with an explicit "check your VPN" message rather than failing silently — but it does not work offline, and that is a genuine regression from the indexed design.

```
┌──────────────────────────────┐
│  Fuseki (dev.minmod.isi.edu) │   GeoChem RDF triples
└──────────────┬───────────────┘
               │  SPARQL, per interaction
               │  (commodity list cached 1h, countries 15m)
               ▼
┌──────────────────────────────┐
│  geochem/queries.py           │   5 functions, escaping, cache, errors
└──────────────┬───────────────┘
               │  list[dict] -> AgGrid rowData
               ▼
┌──────────────────────────────┐
│  Dash app -- By-Product tab   │   commodity -> papers -> deposits -> samples
└──────────────────────────────┘
```

---

## 4. The Commodity Definition Problem

This is the most important modeling decision in the design, flagged directly by Prof. Knoblock: **any commodity that appears anywhere in a paper should be reflected in the filter — not just the ones explicitly labeled as primary or secondary.**

Two candidate sources exist in the data:

| Source | Where it lives | Has a grade attached? |
|---|---|---|
| Primary / secondary commodities | `MineralSite.primary_commodities` / `.secondary_commodities` (curated text, e.g. `"Sn, Pb, Zn, Cu"`) | **No** — plain text, no measurement |
| Measured elements | Every `Element` reachable via `Paper → Site → Sample → Analysis → Element` | **Yes** — each `Element` carries its own `grade` |

**Decision: use measured elements as the single source of truth for the commodity filter.** Two reasons this is the right call, not a simplification for convenience:

1. It directly satisfies the requirement — elements like arsenic that show up in a raw multi-element lab panel but are never called out as a "primary" or "secondary" commodity are exactly the by-products this tab exists to surface.
2. It avoids a real inconsistency: a paper could be listed under a commodity via `primary_commodities` text and then have **nothing to show** when expanded, since that field has no grade attached. Sourcing the filter from `Element` guarantees every paper listed under a commodity has real measurements behind it.

The same principle applies at every level of the drill-down: `get_deposits` returns only sites that actually measured the selected commodity, not every site in the paper. A row that appears is a row you can expand.

`primary_commodities`/`secondary_commodities` are still present in the store as informational context (useful for a site detail view later), but are **not** used to determine which papers appear under a commodity filter.

This decision is now cheaper to honour than it was under the indexed design, where it required a derived `paper_commodities` table. The filter is simply the query:

```sparql
SELECT DISTINCT ?commodity WHERE {
  ?element a gc:Element ;
           rdfs:label ?commodity .
}
```

The `a gc:Element` type guard is load-bearing, not decoration: `rdfs:label` is *also* the site display-name predicate, so without it the commodity dropdown fills up with deposit names like "Mount Isa".

---

## 5. Implementation

Two files:

**`geochem/queries.py`** — the entire data layer. Five functions, each returning plain `list[dict]` handed straight to AgGrid:

| Function | Returns | Bounded? | Cached |
|---|---|---|---|
| `get_commodities()` | element labels | no — full scan | 1 hour |
| `get_countries(commodity)` | country codes | by commodity | 15 min |
| `get_papers(commodity, country=None)` | `paper_uri, title, journal, year, doi` | by commodity | no |
| `get_deposits(paper_uri, commodity)` | `site_uri, deposit_name, country, state, deposit_type_text, deposit_type_confidence` | by paper URI | no |
| `get_samples(site_uri, commodity)` | `measurement_id, sample_id, sample_name, mineral, analysis_id, grade, analytical_method` | by site URI | no |

`get_papers` is deliberately **not** cached: its key space is (commodity × country) and each value grows with the store, which is the wrong thing to hold in a worker process with no memory budget. At 0.6s it is not the bottleneck; if it becomes one, the answer is an index, not a bigger memo.

**`pages/byproduct.py`** — layout and four callbacks forming the drill-down chain. Unchanged in structure from the indexed version; it consumes the same dict keys.

### Details that are load-bearing

**Sorting and null-handling happen in Python, not SPARQL.** `year` is parsed and range-checked in Python, so SPARQL cannot sort on it. Separately, SPARQL's `ORDER BY` places *unbound* values first, whereas the previous SQL placed `NULL`s last — so sorting client-side is what preserves the existing behaviour rather than a departure from it.

**Numeric coercion is required, not polish.** Raw SPARQL JSON returns `mo:confidence` as the string `"9.2E-1"` and some grades in forms like `"9.3e-05"`. Without `to_float()` the grid would display `9.2E-1` and sort the Grade column as text — a silent regression against the old `REAL`/`DOUBLE PRECISION` columns.

**Samples are not aggregated, and dedupe is on the element URI.** One `gc:Element` node is one spot analysis; a single sample can carry 146 repeated measurements of the same element, which is real LA-ICP-MS data and exactly what the table exists to show. The element URI serves as the AgGrid `getRowId`, replacing the old Postgres `BIGSERIAL`. Deduping on it is required rather than defensive: duplicate row ids make AgGrid rows silently overwrite each other.

**Deposit-type fan-out resolves to the highest confidence.** A site may carry several competing `deposit_type_candidate`s, which fans it out into multiple result rows. The most confident is kept, rather than an arbitrary one, so the Confidence column means something.

### Injection safety

SPARQL-over-HTTP has no bind-parameter mechanism, so every value is spliced into query text. Literals and IRIs have different grammars and are handled differently:

- **Literals** (commodity, country) are escaped per SPARQL's `ECHAR` production, backslash first. Verified live: a commodity of `Zn" } UNION { ?s ?p ?o } #` parses as a single literal and returns zero rows.
- **IRIs** (paper_uri, site_uri) are **validated against a whitelist**, not escaped — `^https?://[^\s<>"{}|\\^`]+$`, which is exactly the character class SPARQL's `IRIREF` production forbids. These arrive from grid rows, i.e. from the browser, so they are not necessarily values we produced.

Note that `VALUES ?paper { <uri> }` is **not** a parameter binding — it is still literal text spliced into the query, with the same exposure as inlining the IRI directly. It is used for readability; the safety comes entirely from the validation.

### Failure handling

The endpoint is behind a VPN, so failure is a normal operating condition rather than an edge case. All `requests` failures are converted to a `GeochemQueryError` carrying user-safe text, with the traceback going to the log instead.

This separates two states that the indexed design conflated: **"the query ran and found nothing"** (`No results found.`) versus **"the query could not run"** (`GeoChem data unavailable. Cannot reach the GeoChem data service. Check that you are connected to the ISI VPN.`). Under the old code a VPN outage rendered as "no results", which reads as *this commodity has no data* — wrong and actively misleading.

The commodity dropdown callback degrades to an empty list rather than raising, because if it throws, the page renders nothing at all — not even the layout. Timeouts are 30s bounded / 90s unbounded, far shorter than the old batch extractor's 300s: a batch job can wait, a browser spinner cannot.

---

## 6. Scaling: What Breaks First, and When

Worth keeping distinct when discussing with the team:

**The drill-down levels do not degrade.** `get_deposits` and `get_samples` are pinned by URI; their cost is a function of one paper or one site, which does not grow with the store.

**The commodity-filtered queries degrade in proportion to the commodity's own frequency.** A user selecting Ga or Ge — the actual byproducts this tab exists for — touches a small slice regardless of store size. The bad case is a user selecting Fe or S, present in nearly every analysis, which grows toward tens of seconds at 100× scale. That is the point where an index starts earning its keep.

**The commodity dropdown degrades in proportion to the whole store** and is the first thing that will break. It is cached, so the cost is paid once per process per hour rather than per page view, which buys a lot of headroom — but the cache does not change the underlying trend.

**The trigger to revisit this decision** is `get_commodities()` approaching its 90s timeout, or users routinely filtering on ubiquitous major elements rather than trace byproducts. The fix at that point is not to abandon live querying wholesale: the drill-down queries should stay live regardless. It would be enough to precompute the commodity list and the commodity→paper mapping — which is a far smaller artifact than the five-table index this design replaced, since the ~822K measurements never need to leave the triple store.

---

## 7. Open Questions & Known Limitations

Worth raising with the team explicitly, not silently working around:

- **Repeated measurements per sample+element.** Confirmed real, not a bug — LA-ICP-MS spot analysis; one sample carried 146 separate Ga measurements. Currently every spot is shown as its own row. Needs a team decision: show every spot (current), aggregate to mean/range, or show a count plus range inline. This is a UI question, not a data one.
- **`sample_id` vs `sample_local_id` for "sample number."** The `Sample` class has both. The UI currently displays `sample_id`; needs confirmation against a source paper that this is what "sample number" means as printed.
- **Deposit type is deferred from the filter.** `deposit_type_candidate.observed_name` is often a compound list of competing hypotheses (e.g. *"MVT zinc-lead, Sedimentary Exhalative zinc-lead, Skarn zinc-lead-silver"*) rather than a clean label, with one confidence score covering several hypotheses at once. Whether `normalized_uri` is populated is unconfirmed — needs verification before deposit type can be a real filter.
- **Duplicate deposit-type classifications across sites.** Several distinct sites within the same paper show byte-for-byte identical `observed_name`, `comments`, and `confidence` — suggesting classification ran once per paper/district rather than per site. Worth confirming before presenting per-site confidence as independently assessed.
- **Country coverage is sparse** (~30% of sites), and values are ISO-3 codes (`USA`, `CHN`, `XKX`) rather than readable names. The filter works but under-covers most papers until this improves upstream.
- **`grade_unit` does not resolve to a readable label** in this store. The samples table hardcodes a `Grade(ppm)` column header, which is an assumption rather than a fact read from the data. Units may need resolving via the production MinMod REST API (`/api/v1/units`), or adding to this store directly.
- **`analytical_method` is frequently unpopulated** — the Method column is often blank.
- **VPN dependency at request time.** The tab cannot work without VPN reachability to `dev.minmod.isi.edu:3030`. This is the main cost of removing the index and should be weighed if the dashboard is ever deployed somewhere without that route.
- **No confirmed join key back to production MinMod site records.** An earlier hypothesis (`mo:uri`) turned out to be self-referential, not a cross-reference. This affects whether GeoChem sites can ever be linked to full MinMod site records.

---

## 8. Next Steps

1. Get a team decision on the repeated-spot-analysis display question (§7) — it is the only open item that changes the UI.
2. Confirm `sample_id` vs `sample_local_id` against a source paper.
3. Confirm `deposit_type_candidate.normalized_uri` coverage — determines whether deposit type can become a real filter.
4. Resolve `grade_unit` to a readable label, or confirm ppm is universally correct, before the hardcoded column header misleads anyone.
5. Raise the `gc:paper_year` ingestion gap upstream — the fallback works, but the underlying data loss will affect anything else that needs publication dates.
