# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring

from collections import defaultdict
from unittest import mock

from searx.engines import marginaliaapi
from searx.exceptions import SearxEngineAPIException

from tests import SearxTestCase


def _params(**overrides):
    p = defaultdict(dict)
    p["pageno"] = 1
    p["time_range"] = ""
    p["safesearch"] = 0
    p["searxng_locale"] = "all"
    p["language"] = "all"
    p["headers"] = {}
    p["cookies"] = {}
    p.update(overrides)
    return p


class TestMarginaliaApiEngine(SearxTestCase):
    def setUp(self):
        marginaliaapi.api_key = "TEST-KEY"
        marginaliaapi.results_per_page = 20
        marginaliaapi.domain_count = 2
        marginaliaapi.query_timeout = 150
        marginaliaapi.custom_filter = ""
        marginaliaapi.default_lang = "en"

    # ---- request() ----

    def test_request_sets_basic_params_and_header(self):
        params = _params()
        marginaliaapi.request("hello world", params)
        url = params["url"]
        self.assertTrue(url.startswith("https://api2.marginalia-search.com/search?"))
        self.assertIn("query=hello+world", url)
        self.assertIn("count=20", url)
        self.assertIn("dc=2", url)
        self.assertIn("timeout=150", url)
        self.assertIn("page=1", url)
        self.assertIn("nsfw=0", url)
        self.assertIn("lang=en", url)
        self.assertEqual(params["headers"]["API-Key"], "TEST-KEY")

    def test_request_pagination_passes_page_through(self):
        params = _params(pageno=4)
        marginaliaapi.request("q", params)
        self.assertIn("page=4", params["url"])

    def test_request_safesearch_maps_to_nsfw(self):
        for sx, expected in ((0, "nsfw=0"), (1, "nsfw=1"), (2, "nsfw=1")):
            params = _params(safesearch=sx)
            marginaliaapi.request("q", params)
            self.assertIn(expected, params["url"])

    def test_request_locale_resolves_lang(self):
        params = _params(searxng_locale="de-DE")
        marginaliaapi.request("q", params)
        self.assertIn("lang=de", params["url"])

    def test_request_locale_strips_script_subtag_to_allowed(self):
        # zh-Hans-CN → head=zh, but zh is not in _ALLOWED_LANGS, so falls back
        params = _params(searxng_locale="zh-Hans-CN")
        marginaliaapi.request("q", params)
        self.assertIn("lang=en", params["url"])

    def test_request_locale_unsupported_lang_falls_back(self):
        # ja-JP → head=ja, not in _ALLOWED_LANGS, falls back to default_lang
        params = _params(searxng_locale="ja-JP")
        marginaliaapi.request("q", params)
        self.assertIn("lang=en", params["url"])

    def test_request_locale_allowed_lang(self):
        for locale, expected in (("sv-SE", "sv"), ("fr-FR", "fr"), ("de-DE", "de"), ("en-US", "en")):
            params = _params(searxng_locale=locale)
            marginaliaapi.request("q", params)
            self.assertIn(f"lang={expected}", params["url"])

    def test_request_locale_all_falls_back_to_default(self):
        marginaliaapi.default_lang = "fr"
        params = _params(searxng_locale="all")
        marginaliaapi.request("q", params)
        self.assertIn("lang=fr", params["url"])

    def test_request_custom_filter(self):
        marginaliaapi.custom_filter = "smallweb"
        params = _params()
        marginaliaapi.request("q", params)
        self.assertIn("filter=smallweb", params["url"])

    def test_request_no_filter_when_unset(self):
        params = _params()
        marginaliaapi.request("q", params)
        self.assertNotIn("filter=", params["url"])

    # ---- init() ----

    def test_init_requires_api_key(self):
        marginaliaapi.api_key = ""
        with self.assertRaises(SearxEngineAPIException):
            marginaliaapi.init({})

    def test_init_rejects_invalid_default_lang(self):
        marginaliaapi.api_key = "TEST-KEY"
        marginaliaapi.default_lang = "ja"
        with self.assertRaises(SearxEngineAPIException):
            marginaliaapi.init({})

    def test_init_accepts_valid_default_lang(self):
        marginaliaapi.api_key = "TEST-KEY"
        marginaliaapi.default_lang = "sv"
        marginaliaapi.init({})

    # ---- response() ----

    def _resp(self, payload, status_code=200, text=""):
        r = mock.Mock()
        r.status_code = status_code
        r.json.return_value = payload
        r.text = text
        return r

    def test_response_parses_results(self):
        payload = {
            "license": "CC-BY-NC-SA 4.0",
            "page": 1,
            "pages": 10,
            "query": "plan9",
            "results": [
                {
                    "url": "https://plan9.io/wiki/",
                    "title": "Plan9 wiki",
                    "description": "Plan 9 from Bell Labs.",
                    "quality": 4.47,
                    "format": "html",
                    "resultsFromDomain": 13,
                    "details": [[]],
                }
            ],
        }
        results = list(marginaliaapi.response(self._resp(payload)))
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["url"], "https://plan9.io/wiki/")
        self.assertEqual(r["title"], "Plan9 wiki")
        self.assertEqual(r["content"], "Plan 9 from Bell Labs.")
        self.assertIn("HTML", r["metadata"])
        self.assertIn("quality: 4.47", r["metadata"])
        self.assertIn("results_from_domain: 13", r["metadata"])

    def test_response_skips_results_without_url_or_title(self):
        payload = {
            "results": [
                {"title": "no url", "description": ""},
                {"url": "https://example.com", "description": "no title"},
                {"url": "https://example.com/ok", "title": "ok", "description": ""},
            ]
        }
        results = list(marginaliaapi.response(self._resp(payload)))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://example.com/ok")

    def test_response_omits_single_domain_count(self):
        payload = {
            "results": [
                {"url": "https://e.com", "title": "t", "description": "", "resultsFromDomain": 1},
            ]
        }
        results = list(marginaliaapi.response(self._resp(payload)))
        self.assertNotIn("from domain", results[0]["metadata"])

    def test_response_http_error_raises(self):
        with self.assertRaises(SearxEngineAPIException):
            marginaliaapi.response(self._resp({}, status_code=429, text="Rate limit"))

    def test_response_empty_payload(self):
        results = list(marginaliaapi.response(self._resp({})))
        self.assertEqual(results, [])
