# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring

import datetime
from collections import defaultdict
from unittest import mock

from searx.engines import mojeekapi
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


class TestMojeekApiEngine(SearxTestCase):
    def setUp(self):
        # Reset all module-level engine settings to defaults each test.
        mojeekapi.api_key = "TEST-KEY"
        mojeekapi.results_per_page = 10
        mojeekapi.language_boost = 100
        mojeekapi.region_boost = 10
        mojeekapi.language_restrict = False
        mojeekapi.quality_filter = False
        mojeekapi.title_length = 0
        mojeekapi.snippet_length = 0
        mojeekapi.date_weight = 0
        mojeekapi.exclude_terms = ""
        mojeekapi.include_domains = None
        mojeekapi.exclude_domains = None
        mojeekapi.site = ""
        mojeekapi.cluster_format = 0
        mojeekapi.cluster_results = 0

    # ---- request() ----

    def test_request_sets_api_key_and_basic_params(self):
        params = _params()
        mojeekapi.request("hello world", params)
        self.assertIn("api_key=TEST-KEY", params["url"])
        self.assertIn("fmt=json", params["url"])
        self.assertIn("q=hello+world", params["url"])
        self.assertIn("t=10", params["url"])
        self.assertIn("s=1", params["url"])
        self.assertIn("date=1", params["url"])
        self.assertIn("cdate=1", params["url"])
        self.assertIn("size=1", params["url"])

    def test_request_pagination_is_one_indexed(self):
        params = _params(pageno=3)
        mojeekapi.request("q", params)
        # page 3 at 10 per page → s=21
        self.assertIn("s=21", params["url"])

    def test_request_time_range_native(self):
        for sx in ("day", "month", "year"):
            params = _params(time_range=sx)
            mojeekapi.request("q", params)
            self.assertIn(f"since={sx}", params["url"])

    def test_request_time_range_week_uses_yyyymmdd(self):
        params = _params(time_range="week")
        with mock.patch("searx.engines.mojeekapi.datetime") as dt:
            dt.now.return_value = datetime.datetime(2026, 4, 26)
            # keep the real timedelta available where used
            dt.fromtimestamp = datetime.datetime.fromtimestamp
            mojeekapi.request("q", params)
        self.assertIn("since=20260419", params["url"])

    def test_request_safesearch(self):
        params = _params(safesearch=1)
        mojeekapi.request("q", params)
        self.assertIn("safe=1", params["url"])

        params = _params(safesearch=0)
        mojeekapi.request("q", params)
        self.assertNotIn("safe=", params["url"])

    def test_request_quality_filter_sends_fscr(self):
        mojeekapi.quality_filter = True
        params = _params()
        mojeekapi.request("q", params)
        self.assertIn("fscr=1", params["url"])

        mojeekapi.quality_filter = False
        params = _params()
        mojeekapi.request("q", params)
        self.assertNotIn("fscr=", params["url"])

    def test_request_locale_boosts(self):
        mojeekapi.traits.languages["en"] = "en"
        mojeekapi.traits.regions["en-US"] = "US"
        mojeekapi.traits.custom["language_all"] = ""
        mojeekapi.traits.custom["region_all"] = ""

        params = _params(searxng_locale="en-US")
        mojeekapi.request("q", params)
        self.assertIn("lb=en", params["url"])
        self.assertIn("lbb=100", params["url"])
        self.assertIn("rb=US", params["url"])
        self.assertIn("rbb=10", params["url"])
        self.assertNotIn("lr=", params["url"])

    def test_request_language_restrict_sends_lr(self):
        mojeekapi.traits.languages["en"] = "en"
        mojeekapi.traits.custom["language_all"] = ""
        mojeekapi.traits.custom["region_all"] = ""
        mojeekapi.language_restrict = True

        params = _params(searxng_locale="en-US")
        mojeekapi.request("q", params)
        self.assertIn("lr=en", params["url"])

    def test_request_optional_params(self):
        mojeekapi.exclude_terms = "spam ad"
        mojeekapi.site = "docs.example.com"
        mojeekapi.include_domains = ["a.com", "b.com"]
        mojeekapi.exclude_domains = ["x.com", "y.com"]
        mojeekapi.title_length = 80
        mojeekapi.snippet_length = 240
        mojeekapi.date_weight = 50
        mojeekapi.cluster_format = 2
        mojeekapi.cluster_results = 3

        params = _params()
        mojeekapi.request("q", params)
        url = params["url"]
        self.assertIn("qm=spam+ad", url)
        self.assertIn("site=docs.example.com", url)
        self.assertIn("fi=a.com%2Cb.com", url)
        self.assertIn("fe=x.com%2Cy.com", url)
        self.assertIn("tlen=80", url)
        self.assertIn("dlen=240", url)
        self.assertIn("datewr=50", url)
        self.assertIn("clufmt=2", url)
        self.assertIn("si=3", url)

    def test_request_domain_lists_capped_at_25(self):
        mojeekapi.include_domains = [f"d{i}.com" for i in range(40)]
        params = _params()
        mojeekapi.request("q", params)
        # 25 entries → 24 commas (URL-encoded as %2C)
        self.assertEqual(params["url"].count("%2C"), 24)

    # ---- init() ----

    def test_init_requires_api_key(self):
        mojeekapi.api_key = ""
        with self.assertRaises(SearxEngineAPIException):
            mojeekapi.init({})

    # ---- response() ----

    def _resp(self, payload):
        r = mock.Mock()
        r.status_code = 200
        r.json.return_value = payload
        return r

    def test_response_parses_results(self):
        payload = {
            "response": {
                "status": "OK",
                "results": [
                    {
                        "url": "https://example.com/a",
                        "title": "A title",
                        "desc": "A description",
                        "pdate": 1714003200,  # 2024-04-25 UTC
                        "score": 12.345,
                        "size": "5kb",
                        "image": {"url": "https://t/i.jpg"},
                    }
                ],
            }
        }
        results = list(mojeekapi.response(self._resp(payload)))
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["url"], "https://example.com/a")
        self.assertEqual(r["title"], "A title")
        self.assertEqual(r["content"], "A description")
        self.assertEqual(r["thumbnail"], "https://t/i.jpg")
        self.assertIsInstance(r["publishedDate"], datetime.datetime)
        self.assertIn("score: 12.35", r["metadata"])
        self.assertIn("5kb", r["metadata"])

    def test_response_quality_filter_drops_low_onscr_no_sescr(self):
        mojeekapi.quality_filter = True
        payload = {
            "response": {
                "status": "OK",
                "results": [
                    {"url": "https://lo", "title": "lo", "desc": "", "onscr": 0.05},
                    {"url": "https://hi", "title": "hi", "desc": "", "onscr": 0.25},
                ],
            }
        }
        results = list(mojeekapi.response(self._resp(payload)))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://hi")

    def test_response_quality_filter_keeps_high_sescr(self):
        mojeekapi.quality_filter = True
        payload = {
            "response": {
                "status": "OK",
                "results": [
                    # low onscr but high sescr → kept
                    {"url": "https://kept", "title": "k", "desc": "", "onscr": 0.05, "sescr": 0.9},
                    # low both → dropped
                    {"url": "https://drop", "title": "d", "desc": "", "onscr": 0.05, "sescr": 0.1},
                ],
            }
        }
        results = list(mojeekapi.response(self._resp(payload)))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://kept")

    def test_response_api_error_raises(self):
        payload = {"response": {"status": "ERROR_AUTH", "results": []}}
        with self.assertRaises(SearxEngineAPIException):
            mojeekapi.response(self._resp(payload))

    def test_response_published_date_falls_back_to_crawl(self):
        payload = {
            "response": {
                "status": "OK",
                "results": [
                    {
                        "url": "https://example.com/a",
                        "title": "t",
                        "desc": "",
                        "cdatetimestamp": 1714003200,
                    }
                ],
            }
        }
        results = list(mojeekapi.response(self._resp(payload)))
        self.assertIsInstance(results[0]["publishedDate"], datetime.datetime)

    def test_response_empty_payload(self):
        results = list(mojeekapi.response(self._resp({})))
        self.assertEqual(results, [])
