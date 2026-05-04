# SPDX-License-Identifier: AGPL-3.0-or-later
"""Engine to search using the Marginalia Search API (v2).

.. _Marginalia Search API: https://about.marginalia-search.com/article/api/

Configuration
=============

The engine has the following mandatory setting:

- :py:obj:`api_key`

Optional settings are:

- :py:obj:`results_per_page`
- :py:obj:`domain_count`
- :py:obj:`query_timeout`
- :py:obj:`custom_filter`
- :py:obj:`default_lang`

.. code:: yaml

  - name: marginaliaapi
    engine: marginaliaapi
    api_key: 'YOUR-API-KEY'  # required ('public' for the shared key)
    results_per_page: 20     # optional, 1-100
    domain_count: 2          # optional, 1-100 (max results per domain)
    query_timeout: 150       # optional, 50-250 (ms; query execution limit)
    # custom_filter: 'my-filter'  # optional; v2 server-side filter name
    # default_lang: 'en'              # optional; ISO-639-1 hint (default 'en')

Marginalia is a small, independent search engine focused on the
non-commercial, text-heavy web. The v2 API supports paging, NSFW filtering
and custom server-side filters; see the linked docs for how to obtain a key.

Use the literal value ``public`` as the API key to use the shared rate-limited
key intended for casual access.
"""

import logging
import typing as t
from urllib.parse import urlencode

from searx.exceptions import SearxEngineAPIException
from searx.result_types import EngineResults
from searx.utils import searxng_useragent

logger = logging.getLogger("searx.engines.marginaliaapi")

if t.TYPE_CHECKING:
    from searx.extended_types import SXNG_Response
    from searx.search.processors import OnlineParams

about = {
    "website": "https://marginalia-search.com/",
    "wikidata_id": "Q138685230",
    "official_api_documentation": "https://about.marginalia-search.com/article/api/",
    "use_official_api": True,
    "require_api_key": True,
    "results": "JSON",
}

api_key: str = ""
"""API key for the Marginalia Search API (required). Use ``public`` for the
shared, rate-limited public key."""

categories = ["general", "web"]
paging = True
safesearch = True
time_range_support = False

results_per_page: int = 100
"""Maximum number of results per page (Marginalia ``count``, 1-100)."""

domain_count: int = 2
"""Maximum number of results per domain (Marginalia ``dc``, 1-100)."""

query_timeout: int = 150
"""Per-query execution time limit in milliseconds (Marginalia ``timeout``,
50-250). Higher values give the index more time to find matches but increase
latency."""

custom_filter: str = ""
"""Optional server-side filter name (Marginalia ``filter``). Filters are
created via the v2 ``/filter`` endpoints; leave empty for no filter."""

_ALLOWED_LANGS: frozenset[str] = frozenset({"sv", "en", "fr", "de"})
"""Languages the Marginalia API accepts for the ``lang`` parameter."""

default_lang: str = "en"
"""Default ISO-639-1 language hint sent as ``lang`` when SearXNG's locale
selection is ``all`` or otherwise unresolvable. Must be one of
:py:obj:`_ALLOWED_LANGS`. The API itself defaults to ``en`` server-side."""

base_url = "https://api2.marginalia-search.com"
"""Base URL for the Marginalia Search API (v2)."""


def init(_):
    """Initialize the engine."""
    if not api_key:
        raise SearxEngineAPIException("No API key provided")
    if default_lang not in _ALLOWED_LANGS:
        raise SearxEngineAPIException(
            f"Invalid default_lang {default_lang!r}; expected one of {sorted(_ALLOWED_LANGS)}"
        )


def _resolve_lang(searxng_locale: str | None) -> str:
    """Derive Marginalia's ISO-639-1 ``lang`` hint from a SearXNG locale tag.

    Marginalia's ``lang`` parameter is a single language code (no region),
    so we strip script/region subtags. Falls back to the configured
    :py:obj:`default_lang` when the locale is empty, ``all`` or resolves to
    a language not in :py:obj:`_ALLOWED_LANGS`.
    """
    if not searxng_locale or searxng_locale == "all":
        return default_lang
    head = searxng_locale.replace("_", "-").split("-", 1)[0].lower()
    if len(head) == 2 and head.isalpha() and head in _ALLOWED_LANGS:
        return head
    return default_lang


def request(query: str, params: "OnlineParams") -> None:
    """Create the API request."""
    search_args: dict[str, str | int] = {
        "query": query,
        "count": results_per_page,
        "dc": domain_count,
        "timeout": query_timeout,
        "page": params["pageno"],
        # ``nsfw=1`` filters NSFW content; ``nsfw=0`` disables the filter.
        # Map SearXNG's tri-state safesearch (0/1/2) onto Marginalia's
        # binary toggle: anything above "off" enables filtering.
        "nsfw": 1 if params["safesearch"] > 0 else 0,
        "lang": _resolve_lang(params.get("searxng_locale") or params.get("language") or ""),
    }

    if custom_filter:
        search_args["filter"] = custom_filter

    params["url"] = f"{base_url}/search?{urlencode(search_args)}"
    params["headers"]["API-Key"] = api_key
    params["headers"]["User-Agent"] = searxng_useragent()
    params["headers"]["Accept"] = "application/json"

    logger.debug("marginaliaapi request URL: %s", params["url"])
    # Marginalia returns 429 with a plain-text body when over the rate or
    # daily limit; surface that detail instead of a generic HTTP error.
    params["raise_for_httperror"] = False


_FORMAT_LABELS = {
    "html": "HTML",
    "html5": "HTML5",
    "xhtml": "XHTML",
    "pdf": "PDF",
    "text": "Text",
    "plain": "Text",
}


def _format_label(fmt: str) -> str:
    if not fmt:
        return ""
    return _FORMAT_LABELS.get(fmt.lower(), fmt.upper() if len(fmt) <= 5 else fmt)


def _format_metadata(items: list[tuple[str, str]]) -> str:
    """Format structured metadata items into a ``key: value`` string."""
    return " | ".join(f"{k}: {v}" for k, v in items)


def _metadata_dicts(items: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Convert metadata tuples to ``{"key": ..., "value": ...}`` dicts for API consumers."""
    return [{"key": k, "value": v} for k, v in items]


def response(resp: "SXNG_Response") -> EngineResults:
    """Process the API response and return results."""
    res = EngineResults()

    if resp.status_code != 200:
        text = (resp.text or "").strip()
        detail = f": {text[:200]}" if text else ""
        raise SearxEngineAPIException(f"Marginalia API returned HTTP {resp.status_code}{detail}")

    data = resp.json() or {}

    for result in data.get("results", []) or []:
        url = result.get("url")
        title = result.get("title")
        if not url or not title:
            continue

        metadata_items: list[tuple[str, str]] = []
        fmt_label = _format_label(result.get("format") or "")
        if fmt_label:
            metadata_items.append(("format", fmt_label))
        quality = result.get("quality")
        if isinstance(quality, (int, float)):
            metadata_items.append(("quality", f"{quality:.2f}"))
        results_from_domain = result.get("resultsFromDomain")
        if isinstance(results_from_domain, int) and results_from_domain > 1:
            metadata_items.append(("results_from_domain", str(results_from_domain)))

        res.add(
            res.types.MainResult(
                url=url,
                title=title,
                content=result.get("description") or "",
                metadata=_format_metadata(metadata_items),
                metadata_items=_metadata_dicts(metadata_items),
            ),
        )

    return res
