"""Postgres read queries backing the By-Product dashboard tab.

All commodity/country/paper/deposit/sample lookups go through here -- the
Dash app never talks to SPARQL directly (see geochem/build_geochem_index.py
for how the Postgres index gets populated).
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from constants import GEOCHEM_PG_DSN


def _connect():
    return psycopg.connect(GEOCHEM_PG_DSN, row_factory=dict_row)


def get_commodities() -> list[str]:
    sql = "SELECT DISTINCT commodity_label FROM paper_commodities ORDER BY commodity_label"
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        return [row["commodity_label"] for row in cur.fetchall()]


def get_countries(commodity: str) -> list[str]:
    sql = """
        SELECT DISTINCT s.country
        FROM sites s
        JOIN samples smp     ON smp.site_uri = s.site_uri
        JOIN measurements m  ON m.sample_uri = smp.sample_uri
        WHERE m.element_label = %s AND s.country IS NOT NULL
        ORDER BY s.country
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (commodity,))
        return [row["country"] for row in cur.fetchall()]


def get_papers(commodity: str, country: str | None = None) -> list[dict]:
    base = """
        SELECT DISTINCT p.paper_uri, p.title, p.journal, p.year, p.doi
        FROM papers p
        JOIN paper_commodities pc ON pc.paper_uri = p.paper_uri AND pc.commodity_label = %s
    """
    if country:
        sql = base + """
            WHERE EXISTS (
                SELECT 1 FROM sites s
                JOIN samples smp     ON smp.site_uri = s.site_uri
                JOIN measurements m  ON m.sample_uri = smp.sample_uri
                WHERE s.paper_uri = p.paper_uri AND s.country = %s AND m.element_label = %s
            )
            ORDER BY p.year DESC NULLS LAST, p.title
        """
        params = (commodity, country, commodity)
    else:
        sql = base + "ORDER BY p.year DESC NULLS LAST, p.title"
        params = (commodity,)

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def get_deposits(paper_uri: str, commodity: str) -> list[dict]:
    """Only returns sites with an actual measurement of `commodity` -- consistent
    with paper_commodities' "measured elements only" principle."""
    sql = """
        SELECT DISTINCT
            s.site_uri, s.name AS deposit_name, s.country, s.state,
            s.deposit_type_text, s.deposit_type_confidence
        FROM sites s
        JOIN samples smp     ON smp.site_uri = s.site_uri
        JOIN measurements m  ON m.sample_uri = smp.sample_uri
        WHERE s.paper_uri = %s AND m.element_label = %s
        ORDER BY s.name
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (paper_uri, commodity))
        return cur.fetchall()


def get_samples(site_uri: str, commodity: str) -> list[dict]:
    """One row per analysis/measurement -- NOT aggregated (repeated spot
    analyses, e.g. 146 Ga measurements on one sample, show as 146 rows)."""
    sql = """
        SELECT
            m.id AS measurement_id,
            smp.sample_id, smp.sample_name, smp.mineral,
            m.analysis_id, m.grade, m.analytical_method
        FROM samples smp
        JOIN measurements m ON m.sample_uri = smp.sample_uri
        WHERE smp.site_uri = %s AND m.element_label = %s
        ORDER BY smp.sample_name, m.analysis_id
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (site_uri, commodity))
        return cur.fetchall()
