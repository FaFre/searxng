# SPDX-License-Identifier: AGPL-3.0-or-later
"""Engine to search using the Mojeek Search API.

.. _Mojeek Search API: https://www.mojeek.com/support/api/search/

Configuration
=============

The engine has the following mandatory setting:

- :py:obj:`api_key`

Optional settings are:

- :py:obj:`results_per_page`
- :py:obj:`language_boost`
- :py:obj:`region_boost`
- :py:obj:`language_restrict`
- :py:obj:`quality_filter`
- :py:obj:`title_length`
- :py:obj:`snippet_length`
- :py:obj:`rank_by_date`
- :py:obj:`date_before`
- :py:obj:`exclude_terms`
- :py:obj:`include_domains`
- :py:obj:`exclude_domains`
- :py:obj:`site`
- :py:obj:`cluster_format`
- :py:obj:`cluster_results`
- :py:obj:`confidence_min`

.. code:: yaml

  - name: mojeekapi
    engine: mojeekapi
    api_key: 'YOUR-API-KEY'  # required
    results_per_page: 10     # optional
    language_boost: 100      # optional, 0..100 (0 disables); default 100
    region_boost: 10         # optional, 0..100 (0 disables); default 10
    language_restrict: false # optional; if true sends ``lr`` (hard restrict)
    quality_filter: true     # optional; drops results that fail Mojeek's
                             # recommended onscr/sescr thresholds
    title_length: 80         # optional, 0..127
    snippet_length: 240      # optional, 0..511
    rank_by_date: false      # optional; if true, sort by date (datewr=100)
    date_before: ""          # optional; upper date bound (YYYYMMDD or day|month|year)
    exclude_terms: ""        # optional; words to discard (Mojeek ``qm``)
    include_domains: []      # optional; max 25 domains (``fi``)
    exclude_domains: []      # optional; max 25 domains (``fe``)
    site: ""                 # optional; restrict to a single domain
    cluster_format: 0        # optional; same-host clustering format
    cluster_results: 0       # optional; max results per host (0 = default)

The API supports paging and time filters. When the user has selected a
language or region, the corresponding ``lb``/``rb`` boost parameters are sent
to bias relevance toward that locale (see Mojeek's "Results Scoring" docs).
"""

import logging
import typing as t
from datetime import datetime, timedelta
from urllib.parse import urlencode

from dateutil import parser

from searx.enginelib.traits import EngineTraits
from searx.exceptions import SearxEngineAPIException
from searx.result_types import EngineResults
from searx.utils import html_to_text

logger = logging.getLogger("searx.engines.mojeekapi")

if t.TYPE_CHECKING:
    from searx.extended_types import SXNG_Response
    from searx.search.processors import OnlineParams

traits: EngineTraits = EngineTraits()

about = {
    "website": "https://www.mojeek.com/",
    "wikidata_id": "Q60747299",
    "official_api_documentation": "https://www.mojeek.com/support/api/search/",
    "use_official_api": True,
    "require_api_key": True,
    "results": "JSON",
}

api_key: str = ""
"""API key for the Mojeek Search API (required)."""

categories = ["general", "web"]
paging = True
safesearch = True
time_range_support = True

results_per_page: int = 40
"""Maximum number of results per page (configured to 40 for WebLibre)."""

language_boost: int = 100
"""Strength of the language-relevance boost (``lbb``); Mojeek recommends 100."""

region_boost: int = 10
"""Strength of the region-relevance boost (``rbb``); Mojeek recommends 10."""

language_restrict: bool = False
"""If True, send Mojeek's ``lr`` (beta) parameter to *restrict* results to the
selected language instead of merely boosting it."""

quality_filter: bool = False
"""If True, drop results whose keyword relevance ``onscr`` < 0.15 *and*
semantic match ``sescr`` < 0.5 (Mojeek's recommended low-quality cutoff)."""

title_length: int = 0
"""Optional title length (Mojeek ``tlen``, 0..127). 0 keeps the API default."""

snippet_length: int = 0
"""Optional snippet length (Mojeek ``dlen``, 0..511). 0 keeps the API default."""

rank_by_date: bool = False
"""If True, rank results by date instead of relevance (Mojeek ``datewr=100``).
Mojeek's docs document ``datewr`` with values ``[0|100]`` only — it is a
binary toggle, not a continuous ratio."""

date_before: str = ""
"""Upper date bound (Mojeek ``before``); accepts ``day``/``month``/``year``
or ``YYYYMMDD``. Results are restricted to dates *up to but not including*
this value. Useful for crawled-corpus cut-offs."""

exclude_terms: str = ""
"""Words to discard from results (Mojeek ``qm``); space-separated."""

include_domains: list[str] = []
"""Restrict results to these domains (Mojeek ``fi``); max 25.
Replaced wholesale by SearXNG's YAML loader; never mutated in-place."""

exclude_domains: list[str] = []
"""Drop results from these domains (Mojeek ``fe``); max 25.
Replaced wholesale by SearXNG's YAML loader; never mutated in-place."""

site: str = ""
"""Restrict the search to a single domain (Mojeek ``site``)."""

cluster_format: int = 0
"""Same-host clustering format (Mojeek ``clufmt``). 0 keeps the API default."""

cluster_results: int = 0
"""Max results per host (Mojeek ``si``). 0 keeps the API default."""

confidence_min: int = 0
"""When :py:obj:`quality_filter` is enabled, drop results whose experimental
``cfs`` confidence score (0..5) is below this threshold. 0 disables the
extra check; values >0 are stricter (Mojeek's default ``cfs`` is 5)."""

# Spec bounds — see https://www.mojeek.com/support/api/search/request_parameters.html
# Mojeek doesn't publish a machine-readable spec, so these are tracked manually.
# The HTML page is parsed by ``mojeek.fetch_traits`` for language/region codes;
# numeric bounds are validated locally instead.
_DOMAIN_LIST_MAX = 25  # ``fi``/``fe`` accept up to 25 domains each.
_BOOST_MIN, _BOOST_MAX = 1, 100  # ``lbb``/``rbb`` documented range (0 disables).
_TLEN_MAX = 127  # ``tlen`` (title length) upper bound.
_DLEN_MAX = 511  # ``dlen`` (snippet length) upper bound.
_CLUFMT_MAX = 5  # ``clufmt`` accepts 0..5 (0 = legacy clustering, 1-5 enables new).

# Mojeek's recommended low-quality thresholds (see results_scoring.html).
_ONSCR_MIN = 0.15
_SESCR_MIN = 0.5

base_url = "https://api.mojeek.com/search"
"""Base URL for the Mojeek Search API."""

# Mojeek's ``since`` parameter accepts ``day``, ``month`` and ``year`` directly;
# ``week`` is expressed as a YYYYMMDD lower bound. ``before`` accepts the same
# tokens or a YYYYMMDD upper bound.
_native_since = {"day": "day", "month": "month", "year": "year"}
_week_delta = {"week": timedelta(days=7)}


def _clamp(value: int, lo: int, hi: int) -> int:
    """Clamp *value* to ``[lo, hi]``."""
    return max(lo, min(int(value), hi))


def init(_):
    """Initialize the engine and validate config-supplied bounds."""
    # pylint: disable=global-statement
    global language_boost, region_boost, title_length, snippet_length
    global cluster_format, cluster_results, results_per_page

    if not api_key:
        raise SearxEngineAPIException("No API key provided")

    # Clamp user-supplied settings to spec ranges so a misconfigured YAML
    # doesn't cause the API to silently ignore the parameter or return 4xx.
    if language_boost:
        language_boost = _clamp(language_boost, _BOOST_MIN, _BOOST_MAX)
    if region_boost:
        region_boost = _clamp(region_boost, _BOOST_MIN, _BOOST_MAX)
    if title_length:
        title_length = _clamp(title_length, 0, _TLEN_MAX)
    if snippet_length:
        snippet_length = _clamp(snippet_length, 0, _DLEN_MAX)
    if cluster_format:
        cluster_format = _clamp(cluster_format, 0, _CLUFMT_MAX)
    if cluster_results < 0:
        cluster_results = 0
    if results_per_page < 1:
        results_per_page = 1


def request(query: str, params: "OnlineParams") -> None:
    """Create the API request."""
    search_args: dict[str, str | int] = {
        "q": query,
        "api_key": api_key,
        "fmt": "json",
        "t": results_per_page,
        # ``s`` is 1-indexed: 1 = first result, 11 = second page at 10 per page.
        "s": (params["pageno"] - 1) * results_per_page + 1,
        # request last-modified and crawl dates; ``pdate`` (published) is
        # returned when set. Crawl date is a useful fallback for ``publishedDate``.
        "date": 1,
        "cdate": 1,
        # document size is returned alongside results and surfaced as metadata.
        "size": 1,
    }

    # ``fscr=1`` opts in to detailed scoring signals (``onscr``/``sescr``).
    # Required for ``quality_filter`` to do anything; only available on
    # Mojeek's Custom Plan, so we send it strictly on opt-in.
    if quality_filter:
        search_args["fscr"] = 1

    if exclude_terms:
        search_args["qm"] = exclude_terms
    if site:
        search_args["site"] = site
    if include_domains:
        search_args["fi"] = ",".join(include_domains[:_DOMAIN_LIST_MAX])
    if exclude_domains:
        search_args["fe"] = ",".join(exclude_domains[:_DOMAIN_LIST_MAX])
    if title_length:
        search_args["tlen"] = title_length
    if snippet_length:
        search_args["dlen"] = snippet_length
    if rank_by_date:
        search_args["datewr"] = 100
    if date_before:
        search_args["before"] = date_before
    if cluster_format:
        search_args["clufmt"] = cluster_format
    if cluster_results:
        search_args["si"] = cluster_results

    time_range = params["time_range"]
    if time_range:
        if time_range in _native_since:
            search_args["since"] = _native_since[time_range]
        elif time_range in _week_delta:
            search_args["since"] = (datetime.now() - _week_delta[time_range]).strftime("%Y%m%d")

    if params["safesearch"]:
        search_args["safe"] = 1

    # Locale-aware relevance boosts (see Mojeek "Results Scoring" docs).
    # Resolve via the traits system so SearXNG locale codes map to Mojeek's
    # supported language/region codes (e.g. ``zh-Hans`` → ``zh``). When
    # ``engine_traits.json`` doesn't have an entry for this engine yet
    # (newly added or pre-traits-fetch), fall back to a structural parse of
    # the locale tag — Mojeek's ``lb``/``rb`` accept ISO 639-1 / 3166-1
    # alpha-2 directly, so this still produces working requests.
    language_all = traits.custom.get("language_all", "") if traits.custom else ""
    region_all = traits.custom.get("region_all", "") if traits.custom else ""
    lang_code = traits.get_language(params["searxng_locale"], language_all)
    region_code = traits.get_region(params["searxng_locale"], region_all)

    sxng_locale = params.get("searxng_locale") or ""
    if sxng_locale and sxng_locale != "all":
        parts = sxng_locale.replace("_", "-").split("-")
        struct_lang = parts[0].lower() if parts and parts[0] else ""
        struct_region = ""
        # Pick the last 2-letter alpha segment as the region (skip script
        # subtags like "Hans" in ``zh-Hans-CN``).
        for p in reversed(parts[1:]):
            if len(p) == 2 and p.isalpha():
                struct_region = p.upper()
                break
        if not lang_code or lang_code == language_all:
            if struct_lang:
                lang_code = struct_lang
        if not region_code or region_code == region_all:
            if struct_region:
                region_code = struct_region.lower()  # Mojeek accepts lower-case ISO codes

    if lang_code and lang_code != language_all:
        search_args["lb"] = lang_code
        if language_boost:
            search_args["lbb"] = language_boost
        if language_restrict:
            search_args["lr"] = lang_code
    if region_code and region_code != region_all:
        search_args["rb"] = region_code
        if region_boost:
            search_args["rbb"] = region_boost

    params["url"] = f"{base_url}?{urlencode(search_args)}"

    logger.debug("mojeekapi request URL: %s", params["url"])


def _extract_published_date(published_date_raw: str | int | None):
    """Extract and parse the published/last-modified date from the API response."""
    if not published_date_raw:
        return None

    if isinstance(published_date_raw, (int, float)):
        try:
            return datetime.fromtimestamp(int(published_date_raw))
        except (ValueError, OSError, OverflowError):
            return None

    try:
        return parser.parse(str(published_date_raw))
    except (parser.ParserError, ValueError, OverflowError):
        return None


def _format_metadata(items: list[tuple[str, str]]) -> str:
    """Format structured metadata items into a ``key: value`` string."""
    return " | ".join(f"{k}: {v}" for k, v in items)


def _metadata_dicts(items: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Convert metadata tuples to ``{"key": ..., "value": ...}`` dicts for API consumers."""
    return [{"key": k, "value": v} for k, v in items]


def response(resp: "SXNG_Response") -> EngineResults:
    """Process the API response and return results."""
    res = EngineResults()
    data = resp.json().get("response", {})

    status = data.get("status")
    if status and status != "OK":
        raise SearxEngineAPIException(f"Mojeek API error: {status}")

    for result in data.get("results", []):
        if quality_filter:
            onscr = result.get("onscr")
            sescr = result.get("sescr")
            # Per Mojeek's recommendation:
            #   if sescr is undefined, drop when onscr < 0.15
            #   otherwise drop only when both onscr < 0.15 AND sescr < 0.5
            if isinstance(onscr, (int, float)) and onscr < _ONSCR_MIN:
                if sescr is None or (isinstance(sescr, (int, float)) and sescr < _SESCR_MIN):
                    continue
            # Experimental confidence score; only enforce when configured.
            cfs = result.get("cfs")
            if confidence_min and isinstance(cfs, (int, float)) and cfs < confidence_min:
                continue

        thumbnail = None
        image = result.get("image")
        if isinstance(image, dict):
            thumbnail = image.get("url")

        metadata_items: list[tuple[str, str]] = []
        score = result.get("score")
        if isinstance(score, (int, float)):
            metadata_items.append(("score", f"{score:.2f}"))
        # ``g`` (gravity) and ``nph`` (matched phrase count) are returned only
        # with ``fscr=1`` (Custom Plan); surfaced when present.
        gravity = result.get("g")
        if isinstance(gravity, (int, float)):
            metadata_items.append(("gravity", str(int(gravity))))
        nph = result.get("nph")
        if isinstance(nph, int) and nph > 0:
            metadata_items.append(("phrases", f"{nph} matched"))
        size_str = result.get("size")
        if size_str:
            metadata_items.append(("size", str(size_str)))
        if result.get("mres"):
            metadata_items.append(("more_from_domain", "yes"))

        res.add(
            res.types.MainResult(
                url=result["url"],
                title=html_to_text(result.get("title") or ""),
                content=html_to_text(result.get("desc") or ""),
                publishedDate=_extract_published_date(
                    result.get("pdate") or result.get("timestamp") or result.get("cdatetimestamp")
                ),
                thumbnail=thumbnail,
                metadata=_format_metadata(metadata_items),
                metadata_items=_metadata_dicts(metadata_items),
            ),
        )

    return res


def fetch_traits(engine_traits: EngineTraits):
    """Reuse the scraping engine's traits — the Mojeek web preferences page
    exposes the same ISO 639-1 language codes (``lb``) and ISO 3166-1 alpha-2
    region codes that the API's ``lb``/``rb`` parameters accept."""
    # pylint: disable=import-outside-toplevel
    from searx.engines import mojeek

    mojeek.fetch_traits(engine_traits)
