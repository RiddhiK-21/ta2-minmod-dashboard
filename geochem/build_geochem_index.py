#!/usr/bin/env python3
"""
build_geochem_index.py

Extracts GeoChem paper/site/sample/element data from the Fuseki SPARQL endpoint
and loads it into the Postgres index that backs the By-Product dashboard tab.

Design notes:
  - INCREMENTAL: only papers not already in the `papers` table are processed.
  - BOUNDED: every extraction query is scoped to a small batch of paper URIs via
    SPARQL VALUES. Never run an unbounded query across the whole store -- at
    10,000s of papers that risks timeouts and memory pressure on a shared server.
  - paper_commodities is derived in SQL from measurements, not fetched separately.

Usage (run from the repo root -- constants.py reads critical_minerals.yaml relative to CWD):
    python geochem/build_geochem_index.py                 # incremental
    python geochem/build_geochem_index.py --rebuild       # wipe and reprocess everything
    python geochem/build_geochem_index.py --limit 5       # only process 5 new papers (testing)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Iterator

import psycopg
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from constants import GEOCHEM_SPARQL_ENDPOINT as SPARQL_ENDPOINT
from constants import GEOCHEM_PG_DSN as PG_DSN

PREFIXES = """
PREFIX gc:   <https://geochemistry.isi.edu/ontology/>
PREFIX mo:   <https://minmod.isi.edu/ontology/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""

# Papers per SPARQL batch. Measurements are the heavy part: ~10k elements per
# paper, so 5 papers is roughly 50k result rows per query. Raise if your
# endpoint is comfortable, lower if you see timeouts.
MEASUREMENT_BATCH = 5
METADATA_BATCH = 50


# --------------------------------------------------------------------------
# SPARQL
# --------------------------------------------------------------------------

def sparql(query: str, timeout: int = 300) -> list[dict[str, Any]]:
    """Run a SPARQL SELECT and return rows as flat dicts of strings."""
    resp = requests.post(
        SPARQL_ENDPOINT,
        data={"query": PREFIXES + query},
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/sparql-results+json",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    bindings = resp.json()["results"]["bindings"]
    return [{k: v["value"] for k, v in row.items()} for row in bindings]


def values_clause(uris: list[str]) -> str:
    return " ".join(f"<{u}>" for u in uris)


def chunks(seq: list, n: int) -> Iterator[list]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def to_float(value: str | None) -> float | None:
    """Grades arrive as strings and some are non-numeric (e.g. '<0.5', 'b.d.l.')."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def fetch_all_paper_uris() -> list[str]:
    rows = sparql("SELECT ?paper WHERE { ?paper a gc:MineralResourcePaper }")
    return [r["paper"] for r in rows]


def resolve_year(raw_year: str | None, paper_id: str | None) -> int | None:
    """gc:paper_year exists in the source extraction (confirmed in a raw
    JSON-LD extract -- "paper_year": 2022) but is missing from the RDF
    triples actually loaded into Fuseki for every paper checked so far
    (confirmed live via a full predicate dump -- ingestion drops it
    upstream, not a query issue). Prefer the real predicate so this
    self-corrects once ingestion is fixed; fall back to the leading
    4-digit year every paper_id starts with (e.g. "2004_Ono_etal", or
    "2020A_Cave_etal" when a year/author combo has multiple papers)."""
    if raw_year and raw_year.isdigit():
        year = int(raw_year)
        if 1900 <= year <= 2100:
            return year
    if paper_id and len(paper_id) >= 4 and paper_id[:4].isdigit():
        year = int(paper_id[:4])
        if 1900 <= year <= 2100:
            return year
    return None


def fetch_paper_metadata(paper_uris: list[str]) -> list[dict]:
    rows = sparql(f"""
    SELECT ?paper ?title ?doi ?id ?year ?journal
    WHERE {{
      VALUES ?paper {{ {values_clause(paper_uris)} }}
      OPTIONAL {{ ?paper gc:paper_title   ?title }}
      OPTIONAL {{ ?paper gc:paper_doi     ?doi }}
      OPTIONAL {{ ?paper gc:paper_id      ?id }}
      OPTIONAL {{ ?paper gc:paper_year    ?year }}
      OPTIONAL {{ ?paper gc:paper_journal ?journal }}
    }}
    """)
    # A paper can produce multiple rows if any OPTIONAL field is multi-valued;
    # collapse to one row per paper, first value wins.
    seen: dict[str, dict] = {}
    for r in rows:
        seen.setdefault(r["paper"], r)
    return list(seen.values())


def fetch_sites(paper_uris: list[str]) -> list[dict]:
    # NOTE: mo:name does not exist on Site nodes (confirmed via a live predicate
    # dump) -- the site's display name is rdfs:label. mo:country and
    # mo:state_or_province are not literals; each points to a CandidateEntity
    # node one hop away whose actual value lives on mo:observed_name.
    rows = sparql(f"""
    SELECT ?paper ?site ?name ?country ?state ?primary ?secondary ?dtText ?dtConf
    WHERE {{
      VALUES ?paper {{ {values_clause(paper_uris)} }}
      ?paper gc:has_mineral_site ?site .
      OPTIONAL {{ ?site rdfs:label                ?name }}
      OPTIONAL {{ ?site mo:country/mo:observed_name          ?country }}
      OPTIONAL {{ ?site mo:state_or_province/mo:observed_name ?state }}
      OPTIONAL {{ ?site mo:primary_commodities   ?primary }}
      OPTIONAL {{ ?site mo:secondary_commodities ?secondary }}
      OPTIONAL {{
        ?site mo:deposit_type_candidate ?dt .
        OPTIONAL {{ ?dt mo:observed_name ?dtText }}
        OPTIONAL {{ ?dt mo:confidence    ?dtConf }}
      }}
    }}
    """)
    seen: dict[str, dict] = {}
    for r in rows:
        seen.setdefault(r["site"], r)
    return list(seen.values())


def fetch_measurements(paper_uris: list[str]) -> list[dict]:
    """The heavy query. Walks paper -> site -> sample -> analysis -> element."""
    return sparql(f"""
    SELECT ?site ?sample ?sampleId ?sampleLocalId ?sampleName ?sampleType ?mineral
           ?analysis ?analysisId ?method ?elementLabel ?grade ?gradeUnit ?detectionLimit
    WHERE {{
      VALUES ?paper {{ {values_clause(paper_uris)} }}
      ?paper gc:has_mineral_site ?site .
      ?site  gc:has_sample ?sample .
      OPTIONAL {{ ?sample gc:sample_id       ?sampleId }}
      OPTIONAL {{ ?sample gc:sample_local_id ?sampleLocalId }}
      OPTIONAL {{ ?sample gc:sample_name     ?sampleName }}
      OPTIONAL {{ ?sample gc:sample_type     ?sampleType }}
      OPTIONAL {{ ?sample gc:mineral         ?mineral }}

      ?sample   gc:has_analysis ?analysis .
      OPTIONAL {{ ?analysis gc:analytical_method ?method }}
      OPTIONAL {{ ?analysis gc:analysis_id       ?analysisId }}

      ?analysis gc:element ?element .
      ?element  rdfs:label ?elementLabel .
      OPTIONAL {{ ?element gc:grade           ?grade }}
      OPTIONAL {{ ?element gc:grade_unit      ?gradeUnit }}
      OPTIONAL {{ ?element gc:detection_limit ?detectionLimit }}
    }}
    """)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def upsert_papers(conn, papers: list[dict]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO papers (paper_uri, title, doi, year, journal)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (paper_uri) DO UPDATE
              SET title = EXCLUDED.title,
                  doi = EXCLUDED.doi,
                  year = EXCLUDED.year,
                  journal = EXCLUDED.journal,
                  indexed_at = now()
            """,
            [
                (
                    p["paper"],
                    p.get("title"),
                    p.get("doi"),
                    resolve_year(p.get("year"), p.get("id")),
                    p.get("journal"),
                )
                for p in papers
            ],
        )


def upsert_sites(conn, sites: list[dict]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO sites (site_uri, paper_uri, name, country, state,
                               primary_commodities, secondary_commodities,
                               deposit_type_text, deposit_type_confidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (site_uri) DO NOTHING
            """,
            [
                (
                    s["site"],
                    s["paper"],
                    s.get("name"),
                    s.get("country"),
                    s.get("state"),
                    s.get("primary"),
                    s.get("secondary"),
                    s.get("dtText"),
                    to_float(s.get("dtConf")),
                )
                for s in sites
            ],
        )


def upsert_samples_and_measurements(conn, rows: list[dict]) -> tuple[int, int]:
    """One SPARQL result row = one measurement; samples are deduped from them."""
    samples: dict[str, tuple] = {}
    for r in rows:
        samples.setdefault(
            r["sample"],
            (
                r["sample"],
                r["site"],
                r.get("sampleId"),
                r.get("sampleLocalId"),
                r.get("sampleName"),
                r.get("sampleType"),
                r.get("mineral"),
            ),
        )

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO samples (sample_uri, site_uri, sample_id,
                                 sample_local_id, sample_name, sample_type, mineral)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (sample_uri) DO NOTHING
            """,
            list(samples.values()),
        )

        cur.executemany(
            """
            INSERT INTO measurements (sample_uri, analysis_uri, analysis_id,
                                      analytical_method, element_label, grade,
                                      grade_raw, grade_unit_uri, detection_limit)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    r["sample"],
                    r.get("analysis"),
                    r.get("analysisId"),
                    r.get("method"),
                    r.get("elementLabel"),
                    to_float(r.get("grade")),
                    # keep the original string only when it failed to parse
                    r.get("grade") if to_float(r.get("grade")) is None else None,
                    r.get("gradeUnit"),
                    to_float(r.get("detectionLimit")),
                )
                for r in rows
            ],
        )

    return len(samples), len(rows)


def rebuild_paper_commodities(conn) -> int:
    """Derived entirely in SQL -- no SPARQL round trip needed."""
    with conn.cursor() as cur:
        cur.execute("TRUNCATE paper_commodities")
        cur.execute("""
            INSERT INTO paper_commodities (paper_uri, commodity_label)
            SELECT DISTINCT s.paper_uri, m.element_label
            FROM measurements m
            JOIN samples smp ON smp.sample_uri = m.sample_uri
            JOIN sites   s   ON s.site_uri = smp.site_uri
            WHERE m.element_label IS NOT NULL
            ON CONFLICT DO NOTHING
        """)
        cur.execute("SELECT COUNT(*) FROM paper_commodities")
        return cur.fetchone()[0]


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true",
                    help="Wipe all tables and reprocess every paper")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only process N new papers (for testing)")
    args = ap.parse_args()

    started = time.time()

    with psycopg.connect(PG_DSN) as conn:
        if args.rebuild:
            print("--rebuild: wiping existing data")
            with conn.cursor() as cur:
                cur.execute("TRUNCATE papers CASCADE")
            conn.commit()

        print("Fetching paper list from SPARQL...")
        all_papers = fetch_all_paper_uris()
        print(f"  {len(all_papers)} papers in the triple store")

        with conn.cursor() as cur:
            cur.execute("SELECT paper_uri FROM papers")
            already = {row[0] for row in cur.fetchall()}

        new_papers = [p for p in all_papers if p not in already]
        if args.limit:
            new_papers = new_papers[:args.limit]

        print(f"  {len(already)} already indexed, {len(new_papers)} to process")
        if not new_papers:
            print("Nothing to do.")
            return 0

        # --- metadata + sites (light) ---
        for batch in chunks(new_papers, METADATA_BATCH):
            upsert_papers(conn, fetch_paper_metadata(batch))
            upsert_sites(conn, fetch_sites(batch))
            conn.commit()
        print(f"  papers + sites loaded")

        # --- measurements (heavy) ---
        total_samples = total_meas = 0
        batches = list(chunks(new_papers, MEASUREMENT_BATCH))
        for i, batch in enumerate(batches, 1):
            t0 = time.time()
            rows = fetch_measurements(batch)
            n_samples, n_meas = upsert_samples_and_measurements(conn, rows)
            conn.commit()
            total_samples += n_samples
            total_meas += n_meas
            print(f"  batch {i}/{len(batches)}: "
                  f"{n_samples} samples, {n_meas} measurements "
                  f"({time.time() - t0:.1f}s)")

        print("Rebuilding paper_commodities...")
        n_pc = rebuild_paper_commodities(conn)
        conn.commit()

        print()
        print(f"Done in {time.time() - started:.1f}s")
        print(f"  papers processed : {len(new_papers)}")
        print(f"  samples          : {total_samples}")
        print(f"  measurements     : {total_meas}")
        print(f"  paper_commodities: {n_pc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
