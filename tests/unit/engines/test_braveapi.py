# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring

import datetime
import json
from collections import defaultdict
from unittest import mock

from searx.engines import braveapi
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


class TestBraveApiEngine(SearxTestCase):
    def setUp(self):
        braveapi.api_key = "TEST-KEY"
        braveapi.search_type = "web"
        braveapi.results_per_page = 20
        braveapi.extra_snippets = False
        braveapi.result_filter = ""
        braveapi.goggles = ""
        braveapi.include_sponsored = False

    # ---- request() ----

    def test_request_sets_auth_and_basic_params(self):
        params = _params()
        braveapi.request("hello world", params)
        self.assertEqual(params["headers"]["X-Subscription-Token"], "TEST-KEY")
        self.assertEqual(params["headers"]["Accept"], "application/json")
        self.assertEqual(params["headers"]["Accept-Encoding"], "gzip")
        self.assertIn("q=hello+world", params["url"])
        self.assertIn("count=20", params["url"])
        self.assertIn("offset=0", params["url"])
        self.assertIn("safesearch=off", params["url"])

    def test_request_pagination_uses_page_index(self):
        params = _params(pageno=3)
        braveapi.request("q", params)
        self.assertIn("offset=2", params["url"])

    def test_request_pagination_clamped(self):
        params = _params(pageno=999)
        braveapi.request("q", params)
        # _MAX_PAGE = 10 → max offset is 9
        self.assertIn("offset=9", params["url"])

    def test_request_freshness_mapping(self):
        for sx, brave in [("day", "pd"), ("week", "pw"), ("month", "pm"), ("year", "py")]:
            params = _params(time_range=sx)
            braveapi.request("q", params)
            self.assertIn(f"freshness={brave}", params["url"], f"failed for {sx}")

    def test_request_safesearch_levels(self):
        for level, expected in [(0, "off"), (1, "moderate"), (2, "strict")]:
            params = _params(safesearch=level)
            braveapi.request("q", params)
            self.assertIn(f"safesearch={expected}", params["url"])

    def test_request_locale_full_tag(self):
        params = _params(searxng_locale="de-DE")
        braveapi.request("q", params)
        self.assertIn("search_lang=de", params["url"])
        self.assertIn("country=DE", params["url"])
        # ui_lang uses BCP-47 casing (lower-language, upper-region)
        self.assertIn("ui_lang=de-DE", params["url"])

    def test_request_locale_skips_script_subtag(self):
        params = _params(searxng_locale="zh-Hans-CN")
        braveapi.request("q", params)
        self.assertIn("country=CN", params["url"])
        self.assertNotIn("country=HANS", params["url"])

    def test_request_locale_all_omits_locale_args(self):
        params = _params(searxng_locale="all", language="all")
        braveapi.request("q", params)
        self.assertNotIn("search_lang=", params["url"])
        self.assertNotIn("country=", params["url"])
        self.assertNotIn("ui_lang=", params["url"])

    def test_request_extra_snippets_and_goggles_and_filter(self):
        braveapi.extra_snippets = True
        braveapi.result_filter = "web,news"
        braveapi.goggles = "https://example.com/my.goggle"
        params = _params()
        braveapi.request("q", params)
        self.assertIn("extra_snippets=true", params["url"])
        self.assertIn("result_filter=web%2Cnews", params["url"])
        self.assertIn("goggles=https%3A%2F%2Fexample.com%2Fmy.goggle", params["url"])

    # ---- init() ----

    def test_init_requires_api_key(self):
        braveapi.api_key = ""
        with self.assertRaises(SearxEngineAPIException):
            braveapi.init({})

    def test_init_validates_search_type(self):
        braveapi.search_type = "bogus"
        with self.assertRaises(SearxEngineAPIException):
            braveapi.init({})

    # ---- response() ----

    def _resp(self, payload, status_code=200):
        r = mock.Mock()
        r.status_code = status_code
        r.json.return_value = payload
        return r

    def test_response_http_error_raises(self):
        with self.assertRaises(SearxEngineAPIException):
            braveapi.response(self._resp({}, status_code=429))

    def test_response_web_results(self):
        payload = {
            "web": {
                "results": [
                    {
                        "url": "https://example.com/a",
                        "title": "A title",
                        "description": "A description",
                        "page_age": "2026-04-20T12:34:56",
                        "language": "en",
                        "profile": {"long_name": "Example, Inc."},
                        "thumbnail": {"src": "https://t/s.jpg", "original": "https://t/o.jpg"},
                        "extra_snippets": ["snippet one", "snippet two"],
                    },
                    # missing url → skipped
                    {"title": "no url"},
                    # sponsored → skipped (default)
                    {"url": "https://ad", "title": "Ad", "sponsored": True},
                ]
            }
        }
        results = list(braveapi.response(self._resp(payload)))
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["url"], "https://example.com/a")
        self.assertEqual(r["title"], "A title")
        self.assertIn("snippet one", r["content"])
        self.assertIn("snippet two", r["content"])
        self.assertEqual(r["author"], "Example, Inc.")
        self.assertEqual(r["metadata"], "en")
        self.assertEqual(r["thumbnail"], "https://t/s.jpg")
        self.assertEqual(r["publishedDate"], datetime.datetime(2026, 4, 20, 12, 34, 56))

    def test_response_includes_sponsored_when_enabled(self):
        braveapi.include_sponsored = True
        payload = {
            "web": {
                "results": [
                    {"url": "https://ad", "title": "Ad", "sponsored": True},
                ]
            }
        }
        results = list(braveapi.response(self._resp(payload)))
        self.assertEqual(len(results), 1)

    def test_response_videos_uses_video_template(self):
        braveapi.search_type = "videos"
        payload = {
            "videos": {
                "results": [
                    {
                        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                        "title": "Vid",
                        "description": "desc",
                        "thumbnail": {"src": "https://t/s.jpg", "original": "https://t/o.jpg"},
                        "video": {"duration": "3:45", "views": 1234, "creator": "Channel"},
                    }
                ]
            }
        }
        results = list(braveapi.response(self._resp(payload)))
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["template"], "videos.html")
        self.assertEqual(r["author"], "Channel")
        self.assertEqual(r["views"], "1234")
        self.assertEqual(r["length"], datetime.timedelta(minutes=3, seconds=45))
        self.assertEqual(r["img_src"], "https://t/o.jpg")
        self.assertEqual(r["thumbnail"], "https://t/s.jpg")
        # YouTube URLs should yield an iframe_src
        self.assertTrue(r["iframe_src"])

    def test_response_news(self):
        braveapi.search_type = "news"
        payload = {
            "news": {
                "results": [
                    {
                        "url": "https://news.example/x",
                        "title": "Headline",
                        "description": "Lede",
                        "page_age": "2026-04-25T08:00:00",
                        "thumbnail": {"src": "https://t/s.jpg"},
                        "profile": {"name": "ExampleNews"},
                    }
                ]
            }
        }
        results = list(braveapi.response(self._resp(payload)))
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["author"], "ExampleNews")
        self.assertEqual(r["publishedDate"], datetime.datetime(2026, 4, 25, 8, 0, 0))

    def test_response_discussions(self):
        braveapi.search_type = "discussions"
        payload = {
            "discussions": {
                "results": [
                    {
                        "url": "https://forum/q",
                        "title": "Post title",
                        "description": "desc",
                        "data": {
                            "forum_name": "r/example",
                            "num_answers": 7,
                            "question": "What is X?",
                            "top_comment": "Top comment text",
                        },
                    }
                ]
            }
        }
        results = list(braveapi.response(self._resp(payload)))
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertIn("r/example", r["metadata"])
        self.assertIn("7 answers", r["metadata"])
        self.assertIn("What is X?", r["content"])
        self.assertIn("Top comment text", r["content"])

    def test_parse_duration_variants(self):
        self.assertEqual(braveapi._parse_duration("3:45"), datetime.timedelta(minutes=3, seconds=45))
        self.assertEqual(braveapi._parse_duration("1:02:03"), datetime.timedelta(hours=1, minutes=2, seconds=3))
        self.assertEqual(braveapi._parse_duration(125), datetime.timedelta(seconds=125))
        self.assertEqual(braveapi._parse_duration("90"), datetime.timedelta(seconds=90))
        self.assertIsNone(braveapi._parse_duration(""))
        self.assertIsNone(braveapi._parse_duration(None))
        self.assertIsNone(braveapi._parse_duration("not a number"))

    def test_build_locale_no_traits_fallback(self):
        # traits global not populated yet — the structural fallback should kick in
        args = braveapi._build_locale("fr-FR")
        self.assertEqual(args["search_lang"], "fr")
        self.assertEqual(args["country"], "FR")
        self.assertEqual(args["ui_lang"], "fr-FR")

    def test_response_empty_payload(self):
        results = list(braveapi.response(self._resp({})))
        self.assertEqual(results, [])

    def test_response_payload_round_trips_via_json(self):
        # Ensure .json() responses survive round-trip from an actual JSON string
        text = json.dumps({"web": {"results": []}})
        r = mock.Mock()
        r.status_code = 200
        r.json.return_value = json.loads(text)
        self.assertEqual(list(braveapi.response(r)), [])
