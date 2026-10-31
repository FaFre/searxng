# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,disable=missing-class-docstring,invalid-name

import json
import tempfile
from pathlib import Path

import babel
from mock import Mock

import searx.webapp
import searx.search
import searx.search.processors
from searx.result_types._base import MainResult

from searx import weblibre_search_presets
from searx.results import Timing
from searx.preferences import Preferences
from tests import SearxTestCase


class ViewsTestCase(SearxTestCase):  # pylint: disable=too-many-public-methods

    def setUp(self):
        super().setUp()

        # skip init function (no external HTTP request)
        def dummy(*args, **kwargs):  # pylint: disable=unused-argument
            pass

        self.setattr4test(searx.search.processors.PROCESSORS, 'init', dummy)

        # set some defaults
        test_results = [
            MainResult(
                title="First Test",
                url="http://first.test.xyz",
                content="first test content",
                engine="startpage",
            ),
            MainResult(
                title="Second Test",
                url="http://second.test.xyz",
                content="second test content",
                engine="youtube",
            ),
        ]
        for r in test_results:
            r.normalize_result_fields()
        timings = [
            Timing(engine='startpage', total=0.8, load=0.7),
            Timing(engine='youtube', total=0.9, load=0.6),
        ]

        def search_mock(search_self, *args):  # pylint: disable=unused-argument
            search_self.result_container = Mock(
                get_ordered_results=lambda: test_results,
                answers={},
                corrections=set(),
                suggestions=set(),
                infoboxes=[],
                unresponsive_engines=set(),
                results=test_results,
                results_length=lambda: len(test_results),
                get_timings=lambda: timings,
                redirect_url=None,
                engine_data={},
            )
            search_self.search_query.locale = babel.Locale.parse("en-US", sep='-')

        self.setattr4test(searx.search.Search, 'search', search_mock)

        original_preferences_get_value = Preferences.get_value

        def preferences_get_value(preferences_self, user_setting_name: str):
            if user_setting_name == 'theme':
                return 'simple'
            return original_preferences_get_value(preferences_self, user_setting_name)

        self.setattr4test(Preferences, 'get_value', preferences_get_value)

        # to see full diffs
        self.maxDiff = None  # pylint: disable=invalid-name

    def test_index_empty(self):
        result = self.client.post('/')
        self.assertEqual(result.status_code, 200)
        self.assertIn(
            b'<div class="title"><h1>SearXNG</h1></div>',
            result.data,
        )

    def test_index_html_post(self):
        result = self.client.post('/', data={'q': 'test'})
        self.assertEqual(result.status_code, 308)
        self.assertEqual(result.location, '/search')

    def test_index_html_get(self):
        result = self.client.post('/?q=test')
        self.assertEqual(result.status_code, 308)
        self.assertEqual(result.location, '/search?q=test')

    def test_search_empty_html(self):
        result = self.client.post('/search', data={'q': ''})
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'<div class="title"><h1>SearXNG</h1></div>', result.data)

    def test_search_empty_json(self):
        result = self.client.post('/search', data={'q': '', 'format': 'json'})
        self.assertEqual(result.status_code, 400)

    def test_search_empty_csv(self):
        result = self.client.post('/search', data={'q': '', 'format': 'csv'})
        self.assertEqual(result.status_code, 400)

    def test_search_empty_rss(self):
        result = self.client.post('/search', data={'q': '', 'format': 'rss'})
        self.assertEqual(result.status_code, 400)

    def test_search_html(self):
        result = self.client.post('/search', data={'q': 'test'})

        self.assertIn(
            b'<span class="url_o1"><span class="url_i1">http://second.test.xyz</span></span>',
            result.data,
        )
        self.assertIn(
            b'<p class="content">\n    second <span class="highlight">test</span> ',
            result.data,
        )

    def _next_page_form(self, html: bytes) -> bytes:
        form = html[html.index(b'class="next_page"') :]
        return form[: form.index(b'</form>')]

    def test_search_html_pagination_keeps_search_preset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'search_presets.json'
            preset = {
                'label': 'P',
                'description': 'P',
                'engines': ['dummy engine'],
                'weightOverrides': {'dummy engine': 2},
                'goggles': ['tech'],
            }
            path.write_text(json.dumps({'presets': {'p': preset}}), encoding='utf-8')
            weblibre_search_presets.initialize(path)
        self.addCleanup(weblibre_search_presets.initialize)

        result = self.client.post('/search', data={'q': 'test', 'preset': 'p', 'category_general': '1'})
        next_page = self._next_page_form(result.data)

        self.assertIn(b'name="engines" value="dummy engine"', next_page)
        self.assertIn(b'name="weight_overrides" value="dummy engine:2.0"', next_page)
        self.assertIn(b'name="goggle" value="tech"', next_page)
        # categories would add every engine of the category to the list
        self.assertNotIn(b'name="category_', next_page)
        # same for the RSS link and the copyable search URL
        self.assertIn(b'&amp;engines=dummy%20engine', result.data)
        self.assertNotIn(b'&amp;categories=', result.data)

    def test_search_html_pagination_keeps_categories_without_engines(self):
        # the dummy engines are disabled by default, select one with its bang
        # (bang selections use the pseudo category "none")
        result = self.client.post('/search', data={'q': '!gd test'})
        next_page = self._next_page_form(result.data)

        self.assertIn(b'name="category_none"', next_page)
        self.assertNotIn(b'name="engines"', next_page)

    def test_search_html_keeps_categories_next_to_engine_list(self):
        # engines + categories selects the union of both, forms and URLs must repeat both
        result = self.client.post('/search', data={'q': 'test', 'engines': 'dummy engine', 'category_general': '1'})
        next_page = self._next_page_form(result.data)
        downloads = result.data[result.data.index(b'id="apis"') :]

        for html in (next_page, downloads):
            self.assertIn(b'name="engines" value="dummy engine"', html)
            self.assertIn(b'name="category_general"', html)
        self.assertIn(b'&amp;categories=general', result.data)

    def _tag(self, html: bytes, marker: bytes) -> bytes:
        start = html.rindex(b'<', 0, html.index(marker))
        return html[start : html.index(b'>', start) + 1]

    def test_search_form_keeps_config_without_presets(self):
        # no search_presets.json in the test settings folder: presets are disabled
        result = self.client.post(
            '/search',
            data={
                'q': 'test',
                'engines': 'dummy engine',
                'category_general': '1',
                'weight_overrides': 'dummy engine:2',
                'goggle': 'tech',
            },
        )
        self.assertNotIn(b'id="preset"', result.data)
        search_form = result.data[result.data.index(b'<form id="search"') :]
        search_form = search_form[: search_form.index(b'</form>')]

        for marker, value in (
            (b'id="preset-engines"', b'value="dummy engine"'),
            (b'id="preset-weight-overrides"', b'value="dummy engine:2"'),
            (b'id="preset-goggle"', b'value="tech"'),
        ):
            tag = self._tag(search_form, marker)
            self.assertIn(value, tag)
            self.assertNotIn(b'disabled', tag)
        self.assertIn(b'class="preset-extra-category" name="category_general"', search_form)

    def test_index_form_with_engine_list_only_keeps_extra_categories(self):
        def search_form(path: str) -> bytes:
            html = self.client.get(path).data
            form = html[html.index(b'<form id="search"') :]
            return form[: form.index(b'</form>')]

        # the default categories of the preferences would widen the engine list
        form = search_form('/?engines=dummy%20engine')
        self.assertIn(b'name="engines" value="dummy engine"', form)
        self.assertNotIn(b'name="category_', form)

        form = search_form('/?engines=dummy%20engine&category_general=1')
        self.assertEqual(form.count(b'name="category_general"'), 1)

        form = search_form('/')
        self.assertIn(b'name="category_general"', form)

    def test_index_json(self):
        result = self.client.post('/', data={'q': 'test', 'format': 'json'})
        self.assertEqual(result.status_code, 308)

    def test_search_json(self):
        result = self.client.post('/search', data={'q': 'test', 'format': 'json'})
        result_dict = json.loads(result.data.decode())

        self.assertEqual('test', result_dict['query'])
        self.assertEqual(len(result_dict['results']), 2)
        self.assertEqual(result_dict['results'][0]['content'], 'first test content')
        self.assertEqual(result_dict['results'][0]['url'], 'http://first.test.xyz/')

    def test_index_csv(self):
        result = self.client.post('/', data={'q': 'test', 'format': 'csv'})
        self.assertEqual(result.status_code, 308)

    def test_search_csv(self):
        result = self.client.post('/search', data={'q': 'test', 'format': 'csv'})
        self.assertEqual(
            b'title,url,content,host,engine,score,type\r\n'
            + b'First Test,http://first.test.xyz/,first test content,first.test.xyz,startpage,0,result\r\n'
            + b'Second Test,http://second.test.xyz/,second test content,second.test.xyz,youtube,0,result\r\n',
            result.data,
        )

    def test_index_rss(self):
        result = self.client.post('/', data={'q': 'test', 'format': 'rss'})
        self.assertEqual(result.status_code, 308)

    def test_search_rss(self):
        result = self.client.post('/search', data={'q': 'test', 'format': 'rss'})

        self.assertIn(b'<description>Search results for "test" - SearXNG</description>', result.data)

        self.assertIn(b'<title>First Test</title>', result.data)

        self.assertIn(b'<link>http://first.test.xyz/</link>', result.data)

        self.assertIn(b'<description>first test content</description>', result.data)

    def test_redirect_about(self):
        result = self.client.get('/about')
        self.assertEqual(result.status_code, 302)

    def test_info_page(self):
        result = self.client.get('/info/en/search-syntax')
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'<h1>Search syntax</h1>', result.data)

    def test_health(self):
        result = self.client.get('/healthz')
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'OK', result.data)

    def test_preferences(self):
        result = self.client.get('/preferences')
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'<form id="search_form" method="post" action="/preferences"', result.data)
        self.assertIn(b'<div id="categories_container">', result.data)
        self.assertIn(b'<legend id="pref_ui_locale">Interface language</legend>', result.data)

    def test_browser_locale(self):
        result = self.client.get('/preferences', headers={'Accept-Language': 'zh-tw;q=0.8'})
        self.assertEqual(result.status_code, 200)
        self.assertIn(
            b'<option value="zh-Hant-TW" selected="selected">',
            result.data,
            'Interface locale ignored browser preference.',
        )
        self.assertIn(
            b'<option value="zh-Hant-TW" selected="selected">',
            result.data,
            'Search language ignored browser preference.',
        )

    def test_browser_empty_locale(self):
        result = self.client.get('/preferences', headers={'Accept-Language': ''})
        self.assertEqual(result.status_code, 200)
        self.assertIn(
            b'<option value="en" selected="selected">', result.data, 'Interface locale ignored browser preference.'
        )

    def test_locale_occitan(self):
        result = self.client.get('/preferences?locale=oc')
        self.assertEqual(result.status_code, 200)
        self.assertIn(
            b'<option value="oc" selected="selected">', result.data, 'Interface locale ignored browser preference.'
        )

    def test_stats(self):
        result = self.client.get('/stats')
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'<h1>Engine stats</h1>', result.data)

    def test_robots_txt(self):
        result = self.client.get('/robots.txt')
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'Allow: /', result.data)

    def test_opensearch_xml(self):
        result = self.client.get('/opensearch.xml')
        self.assertEqual(result.status_code, 200)
        self.assertIn(
            b'<Description>SearXNG is a metasearch engine that respects your privacy.</Description>', result.data
        )

    def test_favicon(self):
        result = self.client.get('/favicon.ico')
        result.close()
        self.assertEqual(result.status_code, 200)

    def test_config(self):
        result = self.client.get('/config')
        self.assertEqual(result.status_code, 200)
        json_result = result.get_json()
        self.assertTrue(json_result)
