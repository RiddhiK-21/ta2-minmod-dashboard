"""SPARQL read queries backing the By-Product dashboard tab.

Queries the GeoChem Fuseki store live -- there is no Postgres index any more.
The ISI VPN must be reachable at request time, not just at build time.

Query cost splits three ways:

  * get_commodities() is a full scan of every gc:Element in the store. It is
    the only genuinely unbounded query here, and the reason for the cache
    below. If it starts hitting TIMEOUT_UNBOUNDED, that is the signal the
    store has outgrown live querying and needs an index again.
  * get_countries()/get_papers() scale with the *selected commodity's* slice
    of the store, not the store total, because they start from the element
    label and walk up (see _SITES_FOR_COMMODITY).
  * get_deposits()/get_samples() are pinned to one subtree by VALUES, so they
    stay flat however large the store gets.
"""

from __future__ import annotations

import functools
import re
import statistics
import threading
import time

import requests

from constants import GEOCHEM_SPARQL_ENDPOINT
from logger_config import logger

PREFIXES = """
PREFIX gc:   <https://geochemistry.isi.edu/ontology/>
PREFIX mo:   <https://minmod.isi.edu/ontology/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""

# Bounded queries pinned by a URI; measured worst case is under a second, so
# 30s only ever fires on a genuinely broken endpoint. Deliberately far shorter
# than the old batch extractor's 300s -- a batch job can wait, a browser
# spinner can't.
TIMEOUT_BOUNDED = 30
TIMEOUT_UNBOUNDED = 90

_SESSION = requests.Session()


class GeochemQueryError(RuntimeError):
    """Raised when the GeoChem SPARQL endpoint is unreachable or errors.

    Carries a message that is safe to show to a dashboard user; the
    underlying traceback goes to the log instead.
    """


def sparql(query: str, timeout: int = TIMEOUT_UNBOUNDED) -> list[dict]:
    """Run a SPARQL SELECT and return rows as flat dicts of strings."""
    try:
        resp = _SESSION.post(
            GEOCHEM_SPARQL_ENDPOINT,
            data={"query": PREFIXES + query},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/sparql-results+json",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        bindings = resp.json()["results"]["bindings"]
    except requests.exceptions.Timeout:
        raise GeochemQueryError(
            f"The GeoChem data service did not respond within {timeout}s. "
            "It may be under heavy load -- please try again."
        )
    except requests.exceptions.ConnectionError:
        raise GeochemQueryError(
            "Cannot reach the GeoChem data service. "
            "Check that you are connected to the ISI VPN."
        )
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        logger.exception("GeoChem SPARQL returned HTTP %s", status)
        raise GeochemQueryError(
            f"The GeoChem data service returned an error (HTTP {status})."
        )
    except (ValueError, KeyError):
        logger.exception("Malformed response from GeoChem SPARQL endpoint")
        raise GeochemQueryError(
            "The GeoChem data service returned an unreadable response."
        )
    return [{k: v["value"] for k, v in row.items()} for row in bindings]


# --------------------------------------------------------------------------
# Injection safety
#
# SPARQL-over-HTTP has no bind-parameter mechanism, so every value is spliced
# into the query text. Literals and IRIs have different grammars and are
# handled differently: literals are escaped, IRIs are validated.
# --------------------------------------------------------------------------

_LITERAL_ESCAPES = (
    ("\\", "\\\\"),  # must be first, or later escapes get double-escaped
    ('"', '\\"'),
    ("\n", "\\n"),
    ("\r", "\\r"),
    ("\t", "\\t"),
    ("\b", "\\b"),
    ("\f", "\\f"),
)

# Exactly the characters SPARQL's IRIREF production forbids, plus an
# http(s) anchor.
_IRI_RE = re.compile(r'^https?://[^\s<>"{}|\\^`]+$')


def _lit(value: str) -> str:
    """Render a string as a quoted SPARQL literal, including the quotes."""
    if not isinstance(value, str):
        raise GeochemQueryError(f"expected a string literal, got {value!r}")
    for raw, escaped in _LITERAL_ESCAPES:
        value = value.replace(raw, escaped)
    return f'"{value}"'


def _iri(value: str) -> str:
    """Render a string as a SPARQL IRIREF, including the angle brackets.

    A whitelist rather than an escape: paper_uri/site_uri arrive from grid
    rows, i.e. from the browser, so they are not necessarily values we
    produced. Nothing matching this pattern can terminate the <...>.
    """
    if not isinstance(value, str) or not _IRI_RE.match(value):
        raise GeochemQueryError(f"not a valid resource URI: {value!r}")
    return f"<{value}>"


# --------------------------------------------------------------------------
# Cache
#
# The store only changes when someone runs a GeoChem ingest -- days apart,
# never mid-session -- so the TTL is a safety valve against needing a restart
# after a load, not a staleness guard.
# --------------------------------------------------------------------------

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_MAX_ENTRIES = 256


def _ttl_cache(ttl_seconds: int):
    """Tiny thread-safe TTL memo.

    The network call happens outside the lock on purpose: holding it across a
    multi-second SPARQL round trip would serialise every Dash worker thread
    behind one slow query. The cost is that two concurrent misses on the same
    key both hit Fuseki, which is harmless for idempotent reads.

    Note the cache is per process, so a multi-worker gunicorn deployment pays
    one cold miss per worker.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args):
            key = (fn.__name__, args)
            now = time.monotonic()

            with _CACHE_LOCK:
                entry = _CACHE.get(key)
                if entry is not None and now - entry[0] < ttl_seconds:
                    return entry[1]

            value = fn(*args)

            with _CACHE_LOCK:
                if len(_CACHE) >= _MAX_ENTRIES and key not in _CACHE:
                    # Crude FIFO eviction; dicts preserve insertion order.
                    _CACHE.pop(next(iter(_CACHE)))
                _CACHE[key] = (time.monotonic(), value)
            return value

        return wrapper

    return decorator


def clear_cache() -> None:
    """Drop every cached result, e.g. after a fresh GeoChem load."""
    with _CACHE_LOCK:
        _CACHE.clear()


# --------------------------------------------------------------------------
# Value coercion
# --------------------------------------------------------------------------


def to_float(value: str | None) -> float | None:
    """Coerce a SPARQL literal to a float, or None if it isn't numeric.

    Grades arrive as strings and some are non-numeric (e.g. '<0.5',
    'b.d.l.'). This also normalises the scientific notation the store uses
    for confidences ('9.2E-1' -> 0.92), without which the grid would render
    and sort them as text.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def paper_local_id(paper_uri: str) -> str:
    """The paper's slug from its URI, e.g.
    ".../resource/paper__2004-ono-etal" -> "2004-ono-etal".

    This is the only place the publication year still survives: neither
    gc:paper_year nor gc:paper_id has a single triple in the store (verified
    live), so the URI is the last remaining carrier.
    """
    return paper_uri.rsplit("/", 1)[-1].removeprefix("paper__")


def resolve_year(raw_year: str | None, paper_id: str | None) -> int | None:
    """Year of publication, from the first source that actually carries it.

    gc:paper_year exists in the source extraction but is dropped by ingestion
    before it reaches Fuseki, and gc:paper_id -- the documented fallback -- is
    now absent too: both have zero triples in the store. So in practice every
    Year comes from the paper URI slug via paper_local_id(). The predicates
    are still read first so this self-corrects if ingestion is fixed.

    Both the slug ("2004-ono-etal") and the old paper_id form
    ("2004_Ono_etal", or "2020A_Cave_etal" when a year/author combo has
    several papers) start with the 4-digit year, so one parse covers both.
    """
    if raw_year and raw_year.isdigit():
        year = int(raw_year)
        if 1900 <= year <= 2100:
            return year
    if paper_id and len(paper_id) >= 4 and paper_id[:4].isdigit():
        year = int(paper_id[:4])
        if 1900 <= year <= 2100:
            return year
    return None


def _first_per(rows: list[dict], key: str) -> list[dict]:
    """Collapse OPTIONAL fan-out to one row per `key`, first value wins."""
    seen: dict = {}
    for row in rows:
        seen.setdefault(row[key], row)
    return list(seen.values())


def _nulls_last(value: str | None) -> tuple:
    """Sort key putting unbound values last, matching the old SQL's
    NULLS LAST (plain SPARQL ORDER BY puts unbound first)."""
    return (value is None, value or "")


# --------------------------------------------------------------------------
# Queries
#
# The commodity-filtered queries start from the element label and walk *up*
# to the site, rather than starting from the site and using FILTER EXISTS.
# Measured: for a rare commodity the reverse form is ~20x faster, because
# proving absence forces FILTER EXISTS to enumerate every element under every
# non-matching site. The reverse form's cost is bounded by the selected
# commodity's own element count instead of by the store total.
#
# The inner SELECT DISTINCT also collapses tens of thousands of element rows
# down to a few hundred sites/papers *before* the OPTIONALs run, so every
# OPTIONAL is kept outside the subselect.
# --------------------------------------------------------------------------

# `?element a gc:Element` is load-bearing, not decoration: rdfs:label is also
# the site display-name predicate, so without the type guard this returns
# deposit names ("Mount Isa") alongside element symbols.
_COMMODITIES = """
SELECT DISTINCT ?commodity
WHERE {
  ?element a gc:Element ;
           rdfs:label ?commodity .
}
ORDER BY ?commodity
"""

_COUNTRIES = """
SELECT DISTINCT ?country
WHERE {{
  {{
    SELECT DISTINCT ?site WHERE {{
      ?element rdfs:label {commodity} .
      ?site gc:has_sample/gc:has_analysis/gc:element ?element .
    }}
  }}
  ?site mo:location_info/mo:country/mo:observed_name ?country .
}}
ORDER BY ?country
"""

_PAPER_METADATA = """
  OPTIONAL {{ ?paper gc:paper_title   ?title }}
  OPTIONAL {{ ?paper gc:paper_journal ?journal }}
  OPTIONAL {{ ?paper gc:paper_year    ?year }}
  OPTIONAL {{ ?paper gc:paper_id      ?paperId }}
  OPTIONAL {{ ?paper gc:paper_doi     ?doi }}
"""

_PAPERS = """
SELECT ?paper ?title ?journal ?year ?paperId ?doi
WHERE {{
  {{
    SELECT DISTINCT ?paper WHERE {{
      ?element rdfs:label {commodity} .
      ?paper gc:has_mineral_site/gc:has_sample/gc:has_analysis/gc:element ?element .
    }}
  }}
""" + _PAPER_METADATA + "}}"

# Reusing one ?site variable across both patterns is what makes this "a site
# in that country which measured that commodity", rather than two independent
# conditions on the paper.
_PAPERS_BY_COUNTRY = """
SELECT ?paper ?title ?journal ?year ?paperId ?doi
WHERE {{
  {{
    SELECT DISTINCT ?paper WHERE {{
      ?element rdfs:label {commodity} .
      ?site gc:has_sample/gc:has_analysis/gc:element ?element .
      ?site mo:location_info/mo:country/mo:observed_name {country} .
      ?paper gc:has_mineral_site ?site .
    }}
  }}
""" + _PAPER_METADATA + "}}"

# Site display name is rdfs:label -- mo:name does not exist on Site nodes.
# Country/state hang off mo:location_info, not off the site directly, and each
# points at a CandidateEntity, so the readable value is one further hop via
# mo:observed_name. Same path as _COUNTRIES.
_DEPOSITS = """
SELECT ?site ?deposit_name ?country ?state ?deposit_type_text ?deposit_type_confidence
WHERE {{
  {{
    SELECT DISTINCT ?site WHERE {{
      VALUES ?paper {{ {paper_uri} }}
      ?paper gc:has_mineral_site ?site .
      ?element rdfs:label {commodity} .
      ?site gc:has_sample/gc:has_analysis/gc:element ?element .
    }}
  }}
  OPTIONAL {{ ?site rdfs:label ?deposit_name }}
  OPTIONAL {{ ?site mo:location_info/mo:country/mo:observed_name ?country }}
  OPTIONAL {{ ?site mo:location_info/mo:state_or_province/mo:observed_name ?state }}
  OPTIONAL {{
    ?site mo:deposit_type_candidate ?dt .
    OPTIONAL {{ ?dt mo:observed_name ?deposit_type_text }}
    OPTIONAL {{ ?dt mo:confidence    ?deposit_type_confidence }}
  }}
}}
"""

# No DISTINCT, deliberately: one ?element is one spot analysis, and DISTINCT
# would silently collapse two genuinely identical repeat measurements. The
# hops are written out rather than as a property path because ?sample and
# ?analysis are needed as bindings for the OPTIONALs.
_SAMPLES = """
SELECT ?element ?sample_id ?sample_name ?mineral ?analysis_id ?grade ?analytical_method
WHERE {{
  VALUES ?site {{ {site_uri} }}
  ?site     gc:has_sample   ?sample .
  ?sample   gc:has_analysis ?analysis .
  ?analysis gc:element      ?element .
  ?element  rdfs:label      {commodity} .
  OPTIONAL {{ ?sample   gc:sample_id         ?sample_id }}
  OPTIONAL {{ ?sample   gc:sample_name       ?sample_name }}
  OPTIONAL {{ ?sample   gc:mineral           ?mineral }}
  OPTIONAL {{ ?analysis gc:analysis_id       ?analysis_id }}
  OPTIONAL {{ ?analysis gc:analytical_method ?analytical_method }}
  OPTIONAL {{ ?element  gc:grade             ?grade }}
}}
"""


@_ttl_cache(ttl_seconds=3600)
def get_commodities() -> list[str]:
    """Every distinct measured element label, sorted.

    Cached for an hour: this is the full-store scan, and it runs on every
    visit to the tab.
    """
    return [row["commodity"] for row in sparql(_COMMODITIES)]


@_ttl_cache(ttl_seconds=900)
def get_countries(commodity: str) -> list[str]:
    """Countries of the sites that measured `commodity`, sorted.

    Cached because the page re-derives the country options on every change
    of the country dropdown, not just on a change of commodity.
    """
    rows = sparql(_COUNTRIES.format(commodity=_lit(commodity)))
    return [row["country"] for row in rows]


def get_papers(commodity: str, country: str | None = None) -> list[dict]:
    """Papers with an actual measurement of `commodity`, newest first.

    Not cached: the key space is (commodity x country) and each value grows
    with the store, which is the wrong thing to hold in a worker with no
    memory budget. If this gets slow at scale the answer is an index, not a
    bigger memo.
    """
    if country:
        query = _PAPERS_BY_COUNTRY.format(
            commodity=_lit(commodity), country=_lit(country)
        )
    else:
        query = _PAPERS.format(commodity=_lit(commodity))

    papers = [
        {
            "paper_uri": row["paper"],
            "title": row.get("title"),
            "journal": row.get("journal"),
            "year": resolve_year(
                row.get("year"), row.get("paperId") or paper_local_id(row["paper"])
            ),
            "doi": row.get("doi"),
        }
        for row in _first_per(sparql(query), "paper")
    ]
    # year DESC nulls last, then title -- year is derived in Python, so this
    # ordering cannot be pushed into SPARQL.
    papers.sort(
        key=lambda p: (p["year"] is None, -(p["year"] or 0), _nulls_last(p["title"]))
    )
    return papers


def get_deposits(paper_uri: str, commodity: str) -> list[dict]:
    """Deposit sites in `paper_uri` that measured `commodity`.

    Only sites with an actual measurement, consistent with the tab's
    principle that everything listed has a grade behind it.
    """
    rows = sparql(
        _DEPOSITS.format(paper_uri=_iri(paper_uri), commodity=_lit(commodity)),
        timeout=TIMEOUT_BOUNDED,
    )

    # A site may carry several deposit_type_candidates -- they are competing
    # LLM-generated hypotheses -- which fans the site out into several rows.
    # Keep the most confident one rather than an arbitrary one.
    best: dict = {}
    for row in rows:
        confidence = to_float(row.get("deposit_type_confidence"))
        confidence = -1.0 if confidence is None else confidence
        previous = best.get(row["site"])
        if previous is None or confidence > previous[0]:
            best[row["site"]] = (confidence, row)

    deposits = [
        {
            "site_uri": row["site"],
            "deposit_name": row.get("deposit_name"),
            "country": row.get("country"),
            "state": row.get("state"),
            "deposit_type_text": row.get("deposit_type_text"),
            "deposit_type_confidence": to_float(row.get("deposit_type_confidence")),
        }
        for _, row in best.values()
    ]
    deposits.sort(key=lambda d: _nulls_last(d["deposit_name"]))
    return deposits


def get_samples(site_uri: str, commodity: str) -> list[dict]:
    """Every measurement of `commodity` on samples from `site_uri`.

    One row per analysis, NOT aggregated: repeated measurements of the same
    element on one sample (e.g. 146 Ga spots) are real LA-ICP-MS data and are
    exactly what this table exists to show.
    """
    rows = sparql(
        _SAMPLES.format(site_uri=_iri(site_uri), commodity=_lit(commodity)),
        timeout=TIMEOUT_BOUNDED,
    )

    # The element node is the per-measurement identity. Deduping on it is
    # required rather than defensive: AgGrid's getRowId must be unique, and
    # duplicate ids make rows silently overwrite each other.
    samples = [
        {
            "measurement_id": row["element"],
            "sample_id": row.get("sample_id"),
            "sample_name": row.get("sample_name"),
            "mineral": row.get("mineral"),
            "analysis_id": row.get("analysis_id"),
            "grade": to_float(row.get("grade")),
            "analytical_method": row.get("analytical_method"),
        }
        for row in _first_per(rows, "element")
    ]
    samples.sort(
        key=lambda s: (_nulls_last(s["sample_name"]), _nulls_last(s["analysis_id"]))
    )
    return samples


def summarize_grades(samples: list[dict], commodity: str) -> dict:
    """Min/max/avg/median grade of `commodity` over `samples`.

    Pure, and deliberately not a query: the rows come from get_samples(),
    which is already pinned to one site, so the summary describes exactly the
    measurements in the grid rather than re-querying for them. Rows with no
    numeric grade are skipped -- gc:grade is OPTIONAL in _SAMPLES.
    """
    grades = [s["grade"] for s in samples if s.get("grade") is not None]

    if not grades:
        return {
            "commodity": commodity,
            "count": 0,
            "min": None,
            "max": None,
            "avg": None,
            "median": None,
        }

    return {
        "commodity": commodity,
        "count": len(grades),
        "min": min(grades),
        "max": max(grades),
        "avg": statistics.mean(grades),
        "median": statistics.median(grades),
    }
