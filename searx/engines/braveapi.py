# SPDX-License-Identifier: AGPL-3.0-or-later
"""Engine to search using the Brave (WEB) Search API.

.. _Brave Search API: https://api-dashboard.search.brave.com/documentation

Configuration
=============

The engine has the following mandatory setting:

- :py:obj:`api_key`

Optional settings are:

- :py:obj:`results_per_page`
- :py:obj:`extra_snippets`
- :py:obj:`search_type` (``web``, ``news`` or ``videos``)
- :py:obj:`result_filter`
- :py:obj:`goggles`
- :py:obj:`include_sponsored`

.. code:: yaml

  - name: braveapi
    engine: braveapi
    api_key: 'YOUR-API-KEY'  # required
    results_per_page: 20     # optional, 1-20
    extra_snippets: false    # optional
    search_type: web         # optional: web | news | videos
    # result_filter: "web,news,videos"   # optional, comma-separated
    # goggles: "https://.../my.goggle"   # optional, hosted URL or inline rules
    # include_sponsored: false           # optional, drop sponsored entries

Use additional engine instances with ``search_type: news`` / ``videos`` to
expose dedicated news and video categories backed by the same Brave API.

The API supports paging (max 10 pages) and time filters.
"""

import datetime
import logging
import typing as t

from urllib.parse import urlencode
from dateutil import parser

from searx.enginelib.traits import EngineTraits
from searx.exceptions import SearxEngineAPIException
from searx.result_types import EngineResults
from searx.utils import get_embeded_stream_url, html_to_text

logger = logging.getLogger("searx.engines.braveapi")

if t.TYPE_CHECKING:
    from searx.extended_types import SXNG_Response
    from searx.search.processors import OnlineParams

traits: EngineTraits

about = {
    "website": "https://api.search.brave.com/",
    "wikidata_id": None,
    "official_api_documentation": "https://api-dashboard.search.brave.com/documentation",
    "use_official_api": True,
    "require_api_key": True,
    "results": "JSON",
}

api_key: str = ""
"""API key for Brave Search API (required)."""

categories = ["general", "web"]
paging = True
safesearch = True
time_range_support = True

results_per_page: int = 20
"""Maximum number of results per page (1-20, default 20)."""

extra_snippets: bool = False
"""Request up to 5 additional snippets per result (counts against your plan quota)."""

search_type: str = "web"
"""Which result section to parse. One of ``web``, ``news``, ``videos``,
``discussions``, ``faq``, ``infobox`` or ``locations``.

The Brave web-search endpoint returns mixed verticals in a single response;
this setting selects which one this engine instance surfaces. Pair with
``result_filter`` to also drop unrelated sections from the upstream payload.
"""

result_filter: str = ""
"""Optional comma-separated list passed to the API's ``result_filter``
parameter (e.g. ``"web,news,videos"``). Empty means no filter.
"""

goggles: str = ""
"""Optional Brave Goggles for custom ranking. Either a hosted URL or inline
rules — passed verbatim to the ``goggles`` query parameter."""

include_sponsored: bool = False
"""When false, drop results flagged ``sponsored=true`` by the API."""

family_friendly_only: bool = False
"""When true, drop results where the API set ``family_friendly=false``."""

base_url = "https://api.search.brave.com/res/v1/web/search"
"""Base URL for the Brave Search API."""

# Public OpenAPI-style spec endpoint used by the API dashboard. We fetch this
# at engine startup to learn the *currently* allowed values for the enum
# parameters (``country``, ``search_lang``, ``ui_lang``, ``safesearch``) and
# the numeric bounds for ``count``/``offset``. Brave occasionally adds new
# locales; pulling the live spec keeps us aligned without code changes.
_SPEC_URL = "https://api-dashboard.search.brave.com/api-reference/spec?path=/v1/web/search&method=get"

# Brave's `freshness` parameter values, see:
# https://api-dashboard.search.brave.com/app/documentation/web-search/query
time_range_map = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}

# Hard-coded fallbacks: snapshot of the spec's enum/bounds as of 2026-05-04.
# Used only when the live spec fetch fails at startup so the engine still
# rejects values Brave is known not to support.
_FALLBACK_COUNTRIES: frozenset[str] = frozenset(
    "AR AU AT BE BR CA CL DK FI FR DE GR HK IN ID IT JP KR MY MX NL NZ NO CN "
    "PL PT PH RU SA ZA ES SE CH TW TR GB US ALL".split()
)
_FALLBACK_SEARCH_LANGS: frozenset[str] = frozenset(
    "ar eu bn bg ca zh-hans zh-hant hr cs da nl en en-gb et fi fr gl de el gu "
    "he hi hu is it jp kn ko lv lt ms ml mr nb pl pt-br pt-pt pa ro ru sr sk "
    "sl es sv ta te th tr uk vi".split()
)
_FALLBACK_UI_LANGS: frozenset[str] = frozenset(
    "es-AR en-AU de-AT nl-BE fr-BE pt-BR en-CA fr-CA es-CL da-DK fi-FI fr-FR "
    "de-DE el-GR zh-HK en-IN en-ID it-IT ja-JP ko-KR en-MY es-MX nl-NL en-NZ "
    "no-NO zh-CN pl-PL en-PH ru-RU en-ZA es-ES sv-SE fr-CH de-CH zh-TW tr-TR "
    "en-GB en-US es-US".split()
)
_FALLBACK_SAFESEARCH: frozenset[str] = frozenset(("off", "moderate", "strict"))

# Live values, replaced from the API spec at startup; otherwise the fallbacks.
_ALLOWED_COUNTRIES: frozenset[str] = _FALLBACK_COUNTRIES
_ALLOWED_SEARCH_LANGS: frozenset[str] = _FALLBACK_SEARCH_LANGS
_ALLOWED_UI_LANGS: frozenset[str] = _FALLBACK_UI_LANGS
_ALLOWED_SAFESEARCH: frozenset[str] = _FALLBACK_SAFESEARCH

# Numeric bounds learned from the spec. Brave caps ``count`` at 20 and
# ``offset`` at 9 (zero-indexed page number, so up to 10 pages of paging).
_COUNT_MIN: int = 1
_COUNT_MAX: int = 20
_OFFSET_MAX: int = 9
_MAX_PAGE: int = _OFFSET_MAX + 1
_QUERY_MAX_LEN: int = 400

# SearXNG language tags don't align 1:1 with Brave's ``search_lang`` enum:
# Brave uses Norwegian Bokmål (``nb``) over the macrolanguage ``no``, ``jp``
# (not ``ja``) for Japanese, and ``zh-hans``/``zh-hant`` for Chinese script
# variants. This map applies before the enum membership check.
_SEARCH_LANG_REMAP: dict[str, str] = {
    "ja": "jp",
    "no": "nb",
    "nn": "nb",  # Nynorsk → Bokmål (no separate Nynorsk option in spec)
    "iw": "he",
    "in": "id",  # historical aliases
}

# `_VALID_SEARCH_TYPES` is defined below alongside `_DISPATCH`.


def init(_):
    """Initialize the engine."""
    if not api_key:
        raise SearxEngineAPIException("No API key provided")
    if search_type not in _VALID_SEARCH_TYPES:
        raise SearxEngineAPIException(
            f"Invalid search_type {search_type!r}; expected one of {sorted(_VALID_SEARCH_TYPES)}"
        )
    _load_spec()


def _load_spec() -> None:
    """Fetch the Brave API reference spec and extract enum/numeric bounds.

    The dashboard exposes the same JSON shape used to render the API docs.
    Failures fall back to the hardcoded snapshot above — the engine remains
    functional but stops auto-tracking spec changes until the next startup.
    """
    # pylint: disable=global-statement,import-outside-toplevel
    global _ALLOWED_COUNTRIES, _ALLOWED_SEARCH_LANGS, _ALLOWED_UI_LANGS
    global _ALLOWED_SAFESEARCH, _COUNT_MIN, _COUNT_MAX, _OFFSET_MAX, _MAX_PAGE, _QUERY_MAX_LEN

    try:
        from searx.network import get as _get

        resp = _get(_SPEC_URL, timeout=5)
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code}")
        spec = resp.json()
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("braveapi: failed to load spec from %s (%s); using static fallback", _SPEC_URL, exc)
        return

    by_name = {p.get("name"): p for p in spec.get("params") or [] if isinstance(p, dict)}

    def _enum(param_name: str) -> frozenset[str] | None:
        param = by_name.get(param_name) or {}
        ptype = param.get("type") or {}
        if ptype.get("kind") != "enum":
            return None
        opts = ptype.get("options") or []
        if not opts:
            return None
        return frozenset(str(o) for o in opts if o)

    countries = _enum("country")
    if countries:
        _ALLOWED_COUNTRIES = countries
    search_langs = _enum("search_lang")
    if search_langs:
        _ALLOWED_SEARCH_LANGS = search_langs
    ui_langs = _enum("ui_lang")
    if ui_langs:
        _ALLOWED_UI_LANGS = ui_langs
    safesearch_opts = _enum("safesearch")
    if safesearch_opts:
        _ALLOWED_SAFESEARCH = safesearch_opts

    count_type = (by_name.get("count") or {}).get("type") or {}
    if count_type.get("kind") == "integer":
        _COUNT_MIN = int(count_type.get("minimum", _COUNT_MIN))
        _COUNT_MAX = int(count_type.get("maximum", _COUNT_MAX))
    offset_type = (by_name.get("offset") or {}).get("type") or {}
    if offset_type.get("kind") == "integer":
        _OFFSET_MAX = int(offset_type.get("maximum", _OFFSET_MAX))
        _MAX_PAGE = _OFFSET_MAX + 1
    q_type = (by_name.get("q") or {}).get("type") or {}
    if q_type.get("kind") == "string" and q_type.get("maxLength"):
        _QUERY_MAX_LEN = int(q_type["maxLength"])

    logger.debug(
        "braveapi: spec loaded — %d countries, %d search_langs, %d ui_langs, count<=%d, offset<=%d",
        len(_ALLOWED_COUNTRIES),
        len(_ALLOWED_SEARCH_LANGS),
        len(_ALLOWED_UI_LANGS),
        _COUNT_MAX,
        _OFFSET_MAX,
    )


def _normalize_search_lang(lang: str, region: str) -> str:
    """Pick the best ``search_lang`` value from Brave's enum.

    Tries region-qualified variants first (``en-gb``, ``pt-br``, ``zh-hans``)
    and falls back to the bare language. Returns ``""`` if nothing matches —
    the caller drops the parameter, letting Brave use its default (``en``).
    """
    if not lang:
        return ""
    lang = lang.lower()
    region_l = (region or "").lower()
    candidates: list[str] = []
    if lang == "zh":
        # Chinese script variants are encoded as ``zh-hans``/``zh-hant``.
        if region in ("TW", "HK", "MO"):
            candidates.append("zh-hant")
        else:
            candidates.append("zh-hans")
    if region_l:
        candidates.append(f"{lang}-{region_l}")
    candidates.append(_SEARCH_LANG_REMAP.get(lang, lang))
    for c in candidates:
        if c in _ALLOWED_SEARCH_LANGS:
            return c
    return ""


def _normalize_ui_lang(ui_lang: str) -> str:
    """Validate ``ui_lang`` against Brave's enum (BCP-47, ``lang-REGION``).

    The enum is small (39 entries) and skips many plausible combinations
    (e.g. ``de-CA``). Returns ``""`` for anything not in the spec so we
    don't ship requests Brave will reject with 422.
    """
    if not ui_lang:
        return ""
    parts = ui_lang.replace("_", "-").split("-")
    parts[0] = parts[0].lower()
    for i in range(1, len(parts)):
        if len(parts[i]) == 2 and parts[i].isalpha():
            parts[i] = parts[i].upper()
    candidate = "-".join(parts)
    return candidate if candidate in _ALLOWED_UI_LANGS else ""


def _normalize_country(region: str) -> str:
    """Validate the country code; return ``""`` if Brave doesn't support it."""
    if not region:
        return ""
    region = region.upper()
    return region if region in _ALLOWED_COUNTRIES else ""


def _build_locale(searxng_locale: str) -> dict[str, str]:
    """Map a SearXNG locale tag to Brave query parameters.

    Every value is validated against the spec's enum (loaded at startup by
    :py:func:`_load_spec`); unsupported codes are dropped rather than sent,
    which makes Brave reject the whole request with HTTP 422. We prefer
    values from :py:obj:`EngineTraits` (populated by :py:func:`fetch_traits`
    from Brave's settings page) and fall back to a structural parse of the
    SearXNG tag.
    """
    args: dict[str, str] = {}
    if not searxng_locale or searxng_locale == "all":
        return args

    region = ""
    ui_lang = ""

    # Trait-driven path: only used once `fetch_traits` has populated data.
    try:
        engine_region = traits.get_region(searxng_locale, "")  # type: ignore[has-type]
        if engine_region:
            region = engine_region.upper()
        ui_map = (traits.custom or {}).get("ui_lang") if traits else None  # type: ignore[has-type]
        if ui_map:
            from searx import locales  # local import to avoid import cycles in tests

            ui_lang = locales.get_engine_locale(searxng_locale, ui_map, "")
    except (NameError, AttributeError):
        pass

    parts = searxng_locale.replace("_", "-").split("-")
    lang = parts[0].lower()

    if not region:
        # Pick the last 2-letter segment as the region (skip script subtags
        # like "Hans" in ``zh-Hans-CN``).
        candidate = next((p for p in reversed(parts[1:]) if len(p) == 2 and p.isalpha()), "")
        if candidate:
            region = candidate.upper()

    country = _normalize_country(region)
    if country:
        args["country"] = country

    search_lang = _normalize_search_lang(lang, region)
    if search_lang:
        args["search_lang"] = search_lang

    if not ui_lang and region:
        ui_lang = f"{lang}-{region}"
    ui_lang = _normalize_ui_lang(ui_lang)
    if ui_lang:
        args["ui_lang"] = ui_lang

    return args


def request(query: str, params: "OnlineParams") -> None:
    """Create the API request."""
    # Brave caps offsets at ``_OFFSET_MAX`` (zero-indexed page); pages beyond
    # that get clamped to the last available page rather than 422'd.
    pageno = max(1, min(params["pageno"], _MAX_PAGE))
    count = max(_COUNT_MIN, min(int(results_per_page), _COUNT_MAX))

    # Brave rejects queries longer than ``_QUERY_MAX_LEN`` characters; trim
    # rather than fail. We don't enforce the 50-word limit (rare and cheap
    # to surface as a server-side error if it ever bites).
    if len(query) > _QUERY_MAX_LEN:
        query = query[:_QUERY_MAX_LEN]

    search_args: dict[str, str | int] = {
        "q": query,
        "count": count,
        "offset": pageno - 1,
        # Strip Brave's ``<strong>`` highlight markers — SearXNG templates
        # render the title/description as plain text and would otherwise
        # show literal tags.
        "text_decorations": "false",
    }

    if params["time_range"]:
        freshness = time_range_map.get(params["time_range"])
        if freshness:
            search_args["freshness"] = freshness

    safesearch_value = params["safesearch"]
    if safesearch_value == 2:
        ss = "strict"
    elif safesearch_value == 1:
        ss = "moderate"
    else:
        ss = "off"
    if ss in _ALLOWED_SAFESEARCH:
        search_args["safesearch"] = ss

    search_args.update(_build_locale(params.get("searxng_locale") or params.get("language") or ""))

    if extra_snippets:
        search_args["extra_snippets"] = "true"

    if result_filter:
        search_args["result_filter"] = result_filter

    # Per-query goggles from the weblibre_goggles plugin replace the static
    # `goggles:` engine config when present. The plugin only hands off
    # goggles that have a `<name>.goggle.hosted` sibling (a public URL
    # Brave can fetch); goggles without one are applied locally instead
    # because they're too large to ship inline. We emit one repeated
    # `goggles=` parameter per hosted URL, in selection order — Brave
    # ANDs multiple goggles together.
    goggle_payloads: list[str] = []
    engine_data = params.get("engine_data") or {}
    selected = (engine_data.get("weblibre_goggles") or "").strip()
    if selected:
        try:
            from searx.plugins.weblibre_goggles import get_goggle  # local import: optional dep
        except ImportError:
            get_goggle = None  # type: ignore[assignment]
        if get_goggle is not None:
            for name in selected.split(","):
                name = name.strip()
                if not name:
                    continue
                g = get_goggle(name)
                if g is None or not g.hosted_url:
                    continue
                goggle_payloads.append(g.hosted_url)

    if not goggle_payloads and goggles:
        goggle_payloads = [goggles]

    if goggle_payloads:
        # `urlencode(..., doseq=True)` emits repeated `goggles=` params.
        search_args_list = list(search_args.items())
        search_args_list.extend(("goggles", p) for p in goggle_payloads)
        params["url"] = f"{base_url}?{urlencode(search_args_list, doseq=True)}"
    else:
        params["url"] = f"{base_url}?{urlencode(search_args)}"
    params["headers"]["X-Subscription-Token"] = api_key
    params["headers"]["Accept"] = "application/json"
    params["headers"]["Accept-Encoding"] = "gzip"
    # Read the response body ourselves so we can surface Brave's structured
    # error message (e.g. plan-tier rejections return HTTP 422 with details
    # in the JSON body).
    params["raise_for_httperror"] = False

    logger.debug("braveapi request URL: %s", params["url"])


def _parse_iso_date(value: str | None):
    """Parse an ISO 8601 datetime; return ``None`` if not parseable.

    Brave's ``page_age`` is ISO; ``age`` is a humanized relative string
    ("2 days ago") that ``dateutil`` cannot parse — callers can pass it
    here as a fallback and accept the ``None``.
    """
    if not value:
        return None
    try:
        return parser.parse(value)
    except (parser.ParserError, ValueError, TypeError, OverflowError):
        return None


def _published_date(result: dict[str, t.Any]):
    return _parse_iso_date(result.get("page_age")) or _parse_iso_date(result.get("age"))


def _content(result: dict[str, t.Any]) -> str:
    return html_to_text(result.get("description") or "")


def _extra_snippets(result: dict[str, t.Any]) -> list[str]:
    """Return non-empty, HTML-stripped ``extra_snippets`` entries."""
    snippets: list[str] = []
    for raw in result.get("extra_snippets") or []:
        if not raw:
            continue
        text = html_to_text(str(raw)).strip()
        if text:
            snippets.append(text)
    return snippets


def _extend_with_snippets(items: list[tuple[str, str]], result: dict[str, t.Any]) -> None:
    """Append each non-empty extra snippet as its own ``snippet`` metadata item."""
    for snippet in _extra_snippets(result):
        items.append(("snippet", snippet))


def _format_metadata(items: list[tuple[str, str]]) -> str:
    """Format structured metadata items into a ``key: value`` string."""
    return " | ".join(f"{k}: {v}" for k, v in items)


def _metadata_dicts(items: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Convert metadata tuples to ``{"key": ..., "value": ...}`` dicts for API consumers."""
    return [{"key": k, "value": v} for k, v in items]


def _thumbnail(result: dict[str, t.Any]) -> str | None:
    thumb = result.get("thumbnail") or {}
    return thumb.get("src") or thumb.get("original")


def _thumbnail_full(result: dict[str, t.Any]) -> str | None:
    """Return the full-resolution image, falling back to the small preview."""
    thumb = result.get("thumbnail") or {}
    return thumb.get("original") or thumb.get("src")


def _parse_duration(value: t.Any) -> datetime.timedelta | None:
    """Parse Brave's video duration field into a ``timedelta``.

    Accepts ``"H:MM:SS"``, ``"M:SS"`` or a plain number of seconds.
    """
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.timedelta(seconds=int(value))
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        try:
            parts = [int(p) for p in text.split(":")]
        except ValueError:
            return None
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
        return datetime.timedelta(seconds=seconds)
    try:
        return datetime.timedelta(seconds=int(float(text)))
    except ValueError:
        return None


def _author(result: dict[str, t.Any]) -> str:
    profile = result.get("profile") or {}
    return profile.get("long_name") or profile.get("name") or ""


def _rating_text(rating: dict[str, t.Any] | None, prefix: str = "★ ") -> str:
    """Render a rating object (``ratingValue``/``bestRating``/``reviewCount``)."""
    if not isinstance(rating, dict):
        return ""
    value = rating.get("ratingValue") or rating.get("value")
    if value is None:
        return ""
    best = rating.get("bestRating") or rating.get("best")
    count = rating.get("reviewCount") or rating.get("count")
    text = f"{prefix}{value}/{best}" if best else f"{prefix}{value}"
    if count:
        text += f" ({count})"
    return text


def _names(items: t.Any) -> list[str]:
    """Extract ``name`` from a list of ``{name, ...}`` objects or pass strings through."""
    if not isinstance(items, (list, tuple)):
        return []
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            n = item.get("name") or item.get("long_name")
            if n:
                out.append(str(n))
        elif item:
            out.append(str(item))
    return out


def _rich_type_items(result: dict[str, t.Any]) -> list[tuple[str, str]]:
    """Pull schema.org-style fields out of a web result into structured metadata items.

    Brave attaches optional ``article`` / ``book`` / ``movie`` / ``software``
    / ``recipe`` / ``product`` / ``rating`` / ``organization`` / ``qa``
    blocks per result; we surface each as a ``(key, value)`` pair for
    machine-readable metadata formatting.
    """
    items: list[tuple[str, str]] = []

    article = result.get("article") or {}
    if article:
        authors = _names(article.get("author"))
        if authors:
            items.append(("author", ", ".join(authors)))
        publisher = (article.get("publisher") or {}).get("name")
        if publisher:
            items.append(("publisher", str(publisher)))
        if article.get("date"):
            items.append(("date", str(article["date"])))

    book = result.get("book") or {}
    if book:
        authors = _names(book.get("author"))
        if authors:
            items.append(("author", ", ".join(authors)))
        if book.get("pages"):
            items.append(("pages", f"{book['pages']} pages"))
        price = book.get("price") or {}
        if isinstance(price, dict) and price.get("price"):
            items.append(("price", f"{price['price']} {price.get('priceCurrency') or ''}".strip()))
        rating_text = _rating_text(book.get("rating"))
        if rating_text:
            items.append(("rating", rating_text))

    movie = result.get("movie") or {}
    if movie:
        if movie.get("release"):
            items.append(("released", str(movie["release"])))
        if movie.get("duration"):
            items.append(("duration", str(movie["duration"])))
        genre = movie.get("genre") or []
        if genre:
            items.append(("genre", ", ".join(str(g) for g in genre if g)))
        rating_text = _rating_text(movie.get("rating"))
        if rating_text:
            items.append(("rating", rating_text))

    software = result.get("software") or {}
    if software:
        if software.get("programmingLanguage"):
            items.append(("language", str(software["programmingLanguage"])))
        if software.get("version"):
            items.append(("version", f"v{software['version']}"))
        if software.get("stars") is not None:
            items.append(("stars", f"★ {software['stars']}"))
        if software.get("forks") is not None:
            items.append(("forks", f"⑂ {software['forks']}"))

    recipe = result.get("recipe") or {}
    if recipe:
        if recipe.get("time"):
            items.append(("time", str(recipe["time"])))
        if recipe.get("servings"):
            items.append(("servings", f"{recipe['servings']} servings"))
        if recipe.get("calories"):
            items.append(("calories", f"{recipe['calories']} cal"))
        rating_text = _rating_text(recipe.get("rating"))
        if rating_text:
            items.append(("rating", rating_text))

    product = result.get("product") or {}
    if product:
        if product.get("price"):
            items.append(("price", str(product["price"])))
        if product.get("category"):
            items.append(("category", str(product["category"])))
        rating_text = _rating_text(product.get("rating"))
        if rating_text:
            items.append(("rating", rating_text))

    # Top-level rating, when no rich type already covered it.
    if not items:
        top_rating = _rating_text(result.get("rating"))
        if top_rating:
            items.append(("rating", top_rating))

    organization = result.get("organization") or {}
    if organization.get("name"):
        items.append(("organization", str(organization["name"])))

    qa = result.get("qa") or {}
    question = qa.get("question") if isinstance(qa, dict) else None
    if question:
        question_text = html_to_text(str(question)).strip()
        if question_text:
            items.append(("question", question_text))

    return items


def _flag_items(result: dict[str, t.Any]) -> list[tuple[str, str]]:
    """Boolean-style flags surfaced as ``type`` metadata items."""
    items: list[tuple[str, str]] = []
    if result.get("is_source_local"):
        items.append(("type", "Local"))
    if result.get("is_source_both"):
        items.append(("type", "Local & Web"))
    if result.get("is_live"):
        items.append(("type", "Live"))
    return items


_DAY_ABBRS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _todays_hours(opening_hours: dict[str, t.Any] | None) -> str:
    """Render today's open hours, e.g. ``"Mon 8:00–22:00"``."""
    if not isinstance(opening_hours, dict):
        return ""
    today = opening_hours.get("current_day") or []
    if not today:
        return ""
    spans: list[str] = []
    label = ""
    for span in today:
        if not isinstance(span, dict):
            continue
        opens = span.get("opens")
        closes = span.get("closes")
        if opens and closes:
            spans.append(f"{opens}–{closes}")
        if not label:
            label = span.get("abbr_name") or span.get("full_name") or ""
    if not spans:
        return ""
    prefix = f"{label} " if label else ""
    return f"{prefix}{', '.join(spans)}"


def _first_review_text(reviews: dict[str, t.Any] | None) -> str:
    """Return a short excerpt from the first review."""
    if not isinstance(reviews, dict):
        return ""
    items = reviews.get("results") or []
    if not items:
        return ""
    first = items[0] or {}
    desc = html_to_text(str(first.get("description") or first.get("title") or "")).strip()
    if not desc:
        return ""
    if len(desc) > 160:
        desc = desc[:157] + "…"
    return f"“{desc}”"


def _zoom_to_delta(zoom_level: t.Any) -> float:
    """Approximate half-width (in degrees) of the viewport at ``zoom_level``.

    Mirrors the Web-Mercator rule of thumb ``180 / 2**zoom`` and clamps to
    a sensible range so corrupt zooms don't produce world-spanning boxes.
    """
    try:
        z = float(zoom_level)
    except (TypeError, ValueError):
        z = 16.0
    z = max(3.0, min(z, 20.0))
    return 180.0 / (2 ** z)


def _add_web(res: EngineResults, result: dict[str, t.Any]) -> None:
    metadata_items: list[tuple[str, str]] = []
    metadata_items.extend(_flag_items(result))
    subtype = result.get("subtype")
    if subtype and subtype not in ("generic", "search_result"):
        metadata_items.append(("subtype", str(subtype).replace("_", " ").title()))
    content_type = result.get("content_type")
    if content_type:
        metadata_items.append(("content_type", str(content_type).replace("_", " ").title()))
    metadata_items.extend(_rich_type_items(result))
    if result.get("language"):
        metadata_items.append(("language", str(result["language"])))
    _extend_with_snippets(metadata_items, result)

    # Prefer ``article.author`` for canonical author attribution.
    article_authors = _names((result.get("article") or {}).get("author"))
    author = article_authors[0] if article_authors else _author(result)

    res.add(
        res.types.MainResult(
            url=result["url"],
            title=html_to_text(result["title"]),
            content=_content(result),
            publishedDate=_published_date(result),
            thumbnail=_thumbnail(result) or "",
            author=author,
            metadata=_format_metadata(metadata_items),
            metadata_items=_metadata_dicts(metadata_items),
        ),
    )


def _add_news(res: EngineResults, result: dict[str, t.Any]) -> None:
    metadata_items: list[tuple[str, str]] = []
    if result.get("breaking"):
        metadata_items.append(("type", "Breaking"))
    metadata_items.extend(_flag_items(result))
    if result.get("language"):
        metadata_items.append(("language", str(result["language"])))
    _extend_with_snippets(metadata_items, result)
    # ``source`` is the publisher's plain name (e.g. "Reuters") — preferred
    # over ``profile.name``/``profile.long_name`` when present.
    author = result.get("source") or _author(result)
    res.add(
        res.types.MainResult(
            url=result["url"],
            title=html_to_text(result["title"]),
            content=_content(result),
            publishedDate=_published_date(result),
            thumbnail=_thumbnail(result) or "",
            author=author,
            metadata=_format_metadata(metadata_items),
            metadata_items=_metadata_dicts(metadata_items),
        ),
    )


def _add_video(res: EngineResults, result: dict[str, t.Any]) -> None:
    video = result.get("video") or {}
    metadata_items: list[tuple[str, str]] = []
    metadata_items.extend(_flag_items(result))
    if video.get("publisher"):
        metadata_items.append(("publisher", str(video["publisher"])))
    tags = video.get("tags") or []
    if tags:
        metadata_items.append(("tags", ", ".join(str(t) for t in tags if t)))
    if video.get("requires_subscription"):
        metadata_items.append(("access", "Subscription"))
    if result.get("language"):
        metadata_items.append(("language", str(result["language"])))
    _extend_with_snippets(metadata_items, result)
    video_author = video.get("author") or {}
    res.add(
        res.types.MainResult(
            template="videos.html",
            url=result["url"],
            title=html_to_text(result["title"]),
            content=_content(result),
            publishedDate=_published_date(result),
            thumbnail=_thumbnail(result) or "",
            img_src=_thumbnail_full(result) or "",
            iframe_src=get_embeded_stream_url(result["url"]) or "",
            author=(
                video.get("creator")
                or video_author.get("long_name")
                or video_author.get("name")
                or _author(result)
            ),
            views=str(video.get("views") or ""),
            length=_parse_duration(video.get("duration")),
            metadata=_format_metadata(metadata_items),
            metadata_items=_metadata_dicts(metadata_items),
        ),
    )


def _add_discussion(res: EngineResults, result: dict[str, t.Any]) -> None:
    data = result.get("data") or {}
    metadata_items: list[tuple[str, str]] = []
    forum = data.get("forum_name")
    if forum:
        metadata_items.append(("forum", str(forum)))
    score = data.get("score")
    if score:
        metadata_items.append(("score", f"↑ {score}"))
    answers = data.get("num_answers")
    if answers is not None:
        metadata_items.append(("answers", f"{answers} answers"))
    if result.get("language"):
        metadata_items.append(("language", str(result["language"])))
    _extend_with_snippets(metadata_items, result)

    body_parts: list[str] = []
    for raw in (data.get("question"), data.get("top_comment"), result.get("description")):
        if not raw:
            continue
        text = html_to_text(str(raw)).strip()
        if text:
            body_parts.append(text)
    content = " — ".join(body_parts)

    res.add(
        res.types.MainResult(
            url=result["url"],
            title=html_to_text(result["title"]),
            content=content,
            publishedDate=_published_date(result),
            thumbnail=_thumbnail(result) or "",
            metadata=_format_metadata(metadata_items),
            metadata_items=_metadata_dicts(metadata_items),
        ),
    )


def _add_faq(res: EngineResults, result: dict[str, t.Any]) -> None:
    question = html_to_text(str(result.get("question") or result.get("title") or "")).strip()
    answer = html_to_text(str(result.get("answer") or result.get("description") or "")).strip()
    url = result.get("url") or ""
    if not url or not question:
        return
    res.add(
        res.types.MainResult(
            url=url,
            title=question,
            content=answer,
        ),
    )


def _normalize_attribute(entry: t.Any) -> dict[str, str] | None:
    """Coerce a Brave infobox attribute into ``{label, value}``.

    Brave returns these as either ``[label, value]`` tuples or
    ``{"label": ..., "value": ...}`` objects, depending on subtype.
    Both fields are stripped of HTML to produce plain text for the
    infobox template.
    """
    if isinstance(entry, dict):
        label = entry.get("label") or entry.get("name") or ""
        value = entry.get("value") or entry.get("text") or ""
    elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
        label, value = entry[0], entry[1]
    else:
        return None
    label = html_to_text(str(label).strip())
    value = html_to_text(str(value).strip())
    if not label and not value:
        return None
    return {"label": label, "value": value}


def _infobox_url(result: dict[str, t.Any]) -> str:
    """Best-effort canonical URL for an infobox entry."""
    url = result.get("url")
    if url:
        return url
    meta = result.get("meta_url") or {}
    scheme = meta.get("scheme") or "https"
    netloc = meta.get("netloc") or meta.get("hostname")
    path = meta.get("path") or ""
    if netloc:
        return f"{scheme}://{netloc}{path}"
    return ""


def _add_infobox(res: EngineResults, result: dict[str, t.Any]) -> None:
    title = html_to_text(result.get("title") or result.get("label") or "")
    if not title:
        return

    primary_url = _infobox_url(result)
    website_url = result.get("website_url") or ""

    content_parts = [html_to_text(result.get("description") or ""), html_to_text(result.get("long_desc") or "")]
    content = " — ".join(p for p in content_parts if p)

    attributes: list[dict[str, str]] = []
    for entry in result.get("attributes") or []:
        norm = _normalize_attribute(entry)
        if norm:
            attributes.append(norm)
    entity_info = result.get("entity_info") or {}
    for entry in entity_info.get("attributes") or []:
        norm = _normalize_attribute(entry)
        if norm:
            attributes.append(norm)

    urls: list[dict[str, str | bool]] = []
    seen_urls: set[str] = set()
    if primary_url:
        urls.append({"title": title, "url": primary_url, "official": True})
        seen_urls.add(primary_url)
    if website_url and website_url not in seen_urls:
        urls.append({"title": "Website", "url": website_url, "official": True})
        seen_urls.add(website_url)
    for provider in result.get("providers") or []:
        p_url = (provider or {}).get("url")
        p_name = (provider or {}).get("name") or (provider or {}).get("long_name") or p_url or ""
        if p_url and p_url not in seen_urls:
            urls.append({"title": p_name, "url": p_url})
            seen_urls.add(p_url)
    for profile in result.get("profiles") or []:
        p_url = (profile or {}).get("url")
        p_name = (profile or {}).get("name") or (profile or {}).get("long_name") or p_url or ""
        if p_url and p_url not in seen_urls:
            urls.append({"title": p_name, "url": p_url})
            seen_urls.add(p_url)

    img_src = ""
    for image in result.get("images") or []:
        if not image or image.get("logo"):
            continue
        img_src = image.get("original") or image.get("url") or image.get("src") or ""
        if img_src:
            break
    if not img_src:
        img_src = _thumbnail_full(result) or ""

    category = result.get("category")
    if category:
        attributes.insert(0, {"label": "Category", "value": str(category).capitalize()})

    for rating in result.get("ratings") or []:
        text = _rating_text(rating, prefix="")
        if text:
            attributes.append({"label": (rating or {}).get("name") or "Rating", "value": text})

    distance = result.get("distance") or {}
    if isinstance(distance, dict) and distance.get("value") is not None:
        attributes.append(
            {
                "label": "Distance",
                "value": f"{distance['value']} {distance.get('units') or ''}".strip(),
            }
        )

    movie = result.get("movie") or {}
    if movie:
        if movie.get("release"):
            attributes.append({"label": "Released", "value": str(movie["release"])})
        if movie.get("duration"):
            attributes.append({"label": "Duration", "value": str(movie["duration"])})
        directors = _names(movie.get("directors"))
        if directors:
            attributes.append({"label": "Director", "value": ", ".join(directors)})
        actors = _names(movie.get("actors"))
        if actors:
            attributes.append({"label": "Cast", "value": ", ".join(actors[:8])})
        genre = movie.get("genre") or []
        if genre:
            attributes.append({"label": "Genre", "value": ", ".join(str(g) for g in genre if g)})
        movie_rating = _rating_text(movie.get("rating"), prefix="")
        if movie_rating:
            attributes.append({"label": "Movie rating", "value": movie_rating})

    subtype = result.get("subtype")
    if subtype and subtype != "generic" and not category:
        attributes.insert(0, {"label": "Type", "value": str(subtype).replace("_", " ").title()})

    for source_url in result.get("found_in_urls") or []:
        if source_url and source_url not in seen_urls:
            urls.append({"title": "Source", "url": source_url})
            seen_urls.add(source_url)

    payload = {
        "infobox": title,
        "id": primary_url or None,
        "content": content,
        "img_src": img_src,
        "attributes": attributes,
        "urls": urls,
    }
    res.add(res.types.LegacyResult(payload))


def _add_location(res: EngineResults, result: dict[str, t.Any]) -> None:
    title = html_to_text(result.get("title") or "")
    if not title:
        return

    # Brave returns ``coordinates`` as either a ``[lat, lng]`` list or, in
    # rare cases, a ``{lat, lng}`` dict. Handle both without crashing.
    raw_coords = result.get("coordinates")
    latitude: t.Any = None
    longitude: t.Any = None
    if isinstance(raw_coords, (list, tuple)) and len(raw_coords) >= 2:
        latitude, longitude = raw_coords[0], raw_coords[1]
    elif isinstance(raw_coords, dict):
        latitude = raw_coords.get("latitude") or raw_coords.get("lat")
        longitude = raw_coords.get("longitude") or raw_coords.get("lng") or raw_coords.get("lon")

    postal = result.get("postal_address") or {}
    address = {
        "name": postal.get("name") or title,
        "house_number": postal.get("streetNumber") or postal.get("house_number"),
        "road": postal.get("streetAddress") or postal.get("road"),
        "locality": postal.get("addressLocality") or postal.get("locality"),
        "postcode": postal.get("postalCode") or postal.get("postcode"),
        "country": postal.get("addressCountry") or postal.get("country"),
    }

    links: list[dict[str, str]] = []
    seen_link_urls: set[str] = set()

    def _push_link(label: str, url: str, url_label: str | None = None) -> None:
        if not url or url in seen_link_urls:
            return
        links.append({"label": label, "url": url, "url_label": url_label or url})
        seen_link_urls.add(url)

    contact = result.get("contact") or {}
    phone = contact.get("telephone") or contact.get("phone") or result.get("phone")
    if phone:
        _push_link("phone", f"tel:{phone}", str(phone))
    website = result.get("website") or result.get("url")
    if website:
        _push_link("website", website)
    email = contact.get("email")
    if email:
        _push_link("email", f"mailto:{email}", email)
    action = result.get("action") or {}
    action_url = action.get("url") if isinstance(action, dict) else None
    if action_url:
        _push_link(str(action.get("type") or "action"), action_url)
    if result.get("provider_url"):
        _push_link("provider", result["provider_url"])
    for profile in result.get("profiles") or []:
        p_url = (profile or {}).get("url")
        p_name = (profile or {}).get("long_name") or (profile or {}).get("name") or ""
        if p_url:
            _push_link(p_name or "profile", p_url)

    boundingbox = None
    geojson: dict[str, t.Any] | None = None
    if latitude is not None and longitude is not None:
        try:
            lat = float(latitude)
            lon = float(longitude)
            delta = _zoom_to_delta(result.get("zoom_level"))
            boundingbox = [lat - delta, lat + delta, lon - delta, lon + delta]
            geojson = {"type": "Point", "coordinates": [lon, lat]}
        except (TypeError, ValueError):
            pass

    metadata_items: list[tuple[str, str]] = []
    rating_text = _rating_text(result.get("rating"))
    if rating_text:
        metadata_items.append(("rating", rating_text))
    if result.get("price_range"):
        metadata_items.append(("price_range", str(result["price_range"])))
    distance = result.get("distance") or {}
    if isinstance(distance, dict) and distance.get("value") is not None:
        unit = distance.get("units") or ""
        metadata_items.append(("distance", f"{distance['value']} {unit}".strip()))
    cats = result.get("categories") or []
    if cats:
        metadata_items.append(("categories", ", ".join(str(c) for c in cats if c)))
    cuisine = result.get("serves_cuisine") or []
    if cuisine:
        metadata_items.append(("cuisine", ", ".join(str(c) for c in cuisine if c)))
    hours = _todays_hours(result.get("opening_hours"))
    if hours:
        metadata_items.append(("hours", hours))
    if result.get("timezone"):
        metadata_items.append(("timezone", str(result["timezone"])))
    review_excerpt = _first_review_text(result.get("reviews"))
    if review_excerpt:
        metadata_items.append(("review", review_excerpt))

    payload: dict[str, t.Any] = {
        "template": "map.html",
        "title": title,
        "url": result.get("url") or website or "",
        "content": html_to_text(str(result.get("description") or "")).strip()
        or postal.get("displayAddress")
        or "",
        "thumbnail": _thumbnail(result) or "",
        "address": address,
        "links": links,
        "type": result.get("icon_category") or result.get("type") or (result.get("subtype") or ""),
        "longitude": longitude,
        "latitude": latitude,
        "boundingbox": boundingbox,
        "geojson": geojson,
        "metadata": _format_metadata(metadata_items),
        "metadata_items": _metadata_dicts(metadata_items),
    }
    if not payload["url"]:
        # The map template requires a clickable URL; fall back to a search link.
        return
    res.add(res.types.LegacyResult(payload))


_DISPATCH: dict[str, tuple[str, t.Callable[[EngineResults, dict[str, t.Any]], None]]] = {
    "web": ("web", _add_web),
    "news": ("news", _add_news),
    "videos": ("videos", _add_video),
    "discussions": ("discussions", _add_discussion),
    "faq": ("faq", _add_faq),
    "infobox": ("infobox", _add_infobox),
    "locations": ("locations", _add_location),
}

_VALID_SEARCH_TYPES = set(_DISPATCH)


def response(resp: "SXNG_Response") -> EngineResults:
    """Process the API response and return results."""
    res = EngineResults()

    if resp.status_code != 200:
        detail = ""
        try:
            body = resp.json()
            errors = (body.get("meta") or {}).get("errors") or body.get("errors")
            if errors:
                detail = f": {errors}"
            elif body.get("message"):
                detail = f": {body['message']}"
        except (ValueError, AttributeError, TypeError):
            text = (resp.text or "").strip()
            if text:
                detail = f": {text[:200]}"
        raise SearxEngineAPIException(f"Brave API returned HTTP {resp.status_code}{detail}")

    data = resp.json()

    query_meta = data.get("query") or {}

    # Surface spellcheck rewrite + applied operators as suggestions on web.
    if search_type == "web":
        altered = query_meta.get("altered")
        original = query_meta.get("original")
        if altered and altered != original:
            res.add(res.types.LegacyResult({"suggestion": altered}))
        ops = query_meta.get("search_operators") or {}
        cleaned = ops.get("cleaned_query") if isinstance(ops, dict) else None
        if ops.get("applied") and cleaned and cleaned != original and cleaned != altered:
            res.add(res.types.LegacyResult({"suggestion": cleaned}))

    if query_meta.get("spellcheck_off"):
        logger.debug("Brave API: spellcheck disabled for this query")

    section_key, add_fn = _DISPATCH[search_type]
    section = data.get(section_key) or {}
    if section.get("mutated_by_goggles"):
        logger.debug("Brave API: results mutated by goggles for section %s", section_key)

    for result in section.get("results", []) or []:
        if not include_sponsored and result.get("sponsored"):
            continue
        if family_friendly_only and result.get("family_friendly") is False:
            continue
        # `infobox`/`faq`/`locations` entries don't always carry a top-level
        # `title`; the type-specific parsers do their own validation.
        if section_key in ("web", "news", "videos") and (not result.get("url") or not result.get("title")):
            continue
        add_fn(res, result)

    # Brave returns infobox/faq sidebar cards in the same payload as web
    # results; surface them on the web instance so callers get the full
    # knowledge panel without needing a separate engine instance + API call.
    if search_type == "web":
        for sidebar_key, sidebar_fn in (("infobox", _add_infobox), ("faq", _add_faq)):
            sidebar = data.get(sidebar_key) or {}
            for entry in sidebar.get("results", []) or []:
                if family_friendly_only and entry.get("family_friendly") is False:
                    continue
                sidebar_fn(res, entry)

    return res


def fetch_traits(engine_traits: EngineTraits):
    """Populate :py:obj:`EngineTraits` for the Brave Search API.

    Reuses Brave's public settings page (used by the scraper engine) to
    enumerate UI languages, then maps each region's official languages
    to ISO 3166-1 country codes for the API's ``country`` parameter.
    """
    # pylint: disable=import-outside-toplevel,too-many-branches
    import babel
    import babel.languages
    from lxml import html

    from searx.locales import language_tag, region_tag
    from searx.network import get
    from searx.utils import js_obj_str_to_python

    engine_traits.custom["ui_lang"] = {}

    resp = get("https://search.brave.com/settings", timeout=5)
    if not resp.ok:
        raise RuntimeError("Response from Brave settings is not OK.")

    dom = html.fromstring(resp.text)
    for option in dom.xpath("//section//option[@value='en-us']/../option"):
        ui_lang = option.get("value")
        try:
            locale = babel.Locale.parse(ui_lang, sep="-")
            sxng_tag = region_tag(locale) if locale.territory else language_tag(locale)
        except babel.UnknownLocaleError:
            continue
        engine_traits.custom["ui_lang"].setdefault(sxng_tag, ui_lang)  # type: ignore[union-attr]

    resp = get(
        "https://cdn.search.brave.com/serp/v2/_app/immutable/chunks/parameters.734c106a.js",
        timeout=5,
    )
    if not resp.ok:
        raise RuntimeError("Response from Brave regions is not OK.")

    country_js = resp.text[resp.text.index("options:{all") + len("options:") :]
    country_js = country_js[: country_js.index("},k={default")]
    lang_map = {"no": "nb"}

    for k, v in js_obj_str_to_python(country_js).items():
        if k == "all":
            engine_traits.all_locale = "all"
            continue
        country_tag = v["value"]
        for lang_tag in babel.languages.get_official_languages(country_tag, de_facto=True):
            lang_tag = lang_map.get(lang_tag, lang_tag)
            try:
                sxng_tag = region_tag(babel.Locale.parse(f"{lang_tag}_{country_tag.upper()}"))
            except babel.UnknownLocaleError:
                continue
            engine_traits.regions.setdefault(sxng_tag, country_tag.upper())
