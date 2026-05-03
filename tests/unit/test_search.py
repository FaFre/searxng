# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,disable=missing-class-docstring,invalid-name

from copy import copy

import searx.search
from searx.search.models import SearchQuery, EngineRef
from searx import settings
from tests import SearxTestCase


SAFESEARCH = 0
PAGENO = 1
PUBLIC_ENGINE_NAME = "dummy engine"  # from the ./settings/test_settings.yml


class SearchQueryTestCase(SearxTestCase):

    def test_repr(self):
        s = SearchQuery('test', [EngineRef('bing', 'general')], 'all', 0, 1, '1', 5.0, 'g')
        self.assertEqual(
            repr(s), "SearchQuery('test', [EngineRef('bing', 'general')], 'all', 0, 1, '1', 5.0, 'g', None, {})"
        )  # noqa

    def test_eq(self):
        s = SearchQuery('test', [EngineRef('bing', 'general')], 'all', 0, 1, None, None, None)
        t = SearchQuery('test', [EngineRef('google', 'general')], 'all', 0, 1, None, None, None)
        self.assertEqual(s, s)
        self.assertNotEqual(s, t)

    def test_copy(self):
        s = SearchQuery('test', [EngineRef('bing', 'general')], 'all', 0, 1, None, None, None)
        t = copy(s)
        self.assertEqual(s, t)

    def test_weight_overrides_eq_and_hash(self):
        a = SearchQuery(
            'q', [EngineRef('bing', 'general')], 'all', 0, 1, None, None, None,
            weight_overrides={'bing': 2.0},
        )
        b = SearchQuery(
            'q', [EngineRef('bing', 'general')], 'all', 0, 1, None, None, None,
            weight_overrides={'bing': 2.0},
        )
        c = SearchQuery(
            'q', [EngineRef('bing', 'general')], 'all', 0, 1, None, None, None,
            weight_overrides={'bing': 0.5},
        )
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))
        self.assertNotEqual(a, c)


class ParseWeightOverridesTestCase(SearxTestCase):
    """End-to-end coverage of weight_overrides flowing through the webadapter."""

    def test_parse_basic(self):
        from searx.webadapter import parse_weight_overrides
        # 'dummy engine' is registered by the test settings fixture
        result = parse_weight_overrides({'weight_overrides': 'dummy engine:2.5'})
        self.assertEqual(result, {'dummy engine': 2.5})

    def test_parse_zero_preserved(self):
        from searx.webadapter import parse_weight_overrides
        self.assertEqual(
            parse_weight_overrides({'weight_overrides': 'dummy engine:0'}),
            {'dummy engine': 0.0},
        )

    def test_parse_rejects_unknown_engine(self):
        from searx.webadapter import parse_weight_overrides
        self.assertEqual(
            parse_weight_overrides({'weight_overrides': 'no_such_engine:2.0'}),
            {},
        )

    def test_parse_rejects_negative_nan_inf(self):
        from searx.webadapter import parse_weight_overrides
        self.assertEqual(
            parse_weight_overrides({'weight_overrides': 'dummy engine:-1'}), {}
        )
        self.assertEqual(
            parse_weight_overrides({'weight_overrides': 'dummy engine:nan'}), {}
        )
        self.assertEqual(
            parse_weight_overrides({'weight_overrides': 'dummy engine:inf'}), {}
        )

    def test_parse_clamps_positive(self):
        from searx.webadapter import parse_weight_overrides, MIN_WEIGHT, MAX_WEIGHT
        self.assertEqual(
            parse_weight_overrides({'weight_overrides': 'dummy engine:1e-12'}),
            {'dummy engine': MIN_WEIGHT},
        )
        self.assertEqual(
            parse_weight_overrides({'weight_overrides': 'dummy engine:1e9'}),
            {'dummy engine': MAX_WEIGHT},
        )

    def test_parse_oversized_input_rejected(self):
        from searx.webadapter import parse_weight_overrides, MAX_WEIGHT_OVERRIDES_RAW_LEN
        raw = 'a:1,' * (MAX_WEIGHT_OVERRIDES_RAW_LEN // 4 + 1)
        self.assertEqual(
            parse_weight_overrides({'weight_overrides': raw}), {}
        )

    def test_parse_empty(self):
        from searx.webadapter import parse_weight_overrides
        self.assertEqual(parse_weight_overrides({}), {})
        self.assertEqual(parse_weight_overrides({'weight_overrides': ''}), {})


class SearchTestCase(SearxTestCase):

    def test_timeout_simple(self):
        settings['outgoing']['max_request_timeout'] = None
        search_query = SearchQuery(
            'test', [EngineRef(PUBLIC_ENGINE_NAME, 'general')], 'en-US', SAFESEARCH, PAGENO, None, None
        )
        search = searx.search.Search(search_query)
        with self.app.test_request_context('/search'):
            search.search()
        self.assertEqual(search.actual_timeout, 3.0)

    def test_timeout_query_above_default_nomax(self):
        settings['outgoing']['max_request_timeout'] = None
        search_query = SearchQuery(
            'test', [EngineRef(PUBLIC_ENGINE_NAME, 'general')], 'en-US', SAFESEARCH, PAGENO, None, 5.0
        )
        search = searx.search.Search(search_query)
        with self.app.test_request_context('/search'):
            search.search()
        self.assertEqual(search.actual_timeout, 3.0)

    def test_timeout_query_below_default_nomax(self):
        settings['outgoing']['max_request_timeout'] = None
        search_query = SearchQuery(
            'test', [EngineRef(PUBLIC_ENGINE_NAME, 'general')], 'en-US', SAFESEARCH, PAGENO, None, 1.0
        )
        search = searx.search.Search(search_query)
        with self.app.test_request_context('/search'):
            search.search()
        self.assertEqual(search.actual_timeout, 1.0)

    def test_timeout_query_below_max(self):
        settings['outgoing']['max_request_timeout'] = 10.0
        search_query = SearchQuery(
            'test', [EngineRef(PUBLIC_ENGINE_NAME, 'general')], 'en-US', SAFESEARCH, PAGENO, None, 5.0
        )
        search = searx.search.Search(search_query)
        with self.app.test_request_context('/search'):
            search.search()
        self.assertEqual(search.actual_timeout, 5.0)

    def test_timeout_query_above_max(self):
        settings['outgoing']['max_request_timeout'] = 10.0
        search_query = SearchQuery(
            'test', [EngineRef(PUBLIC_ENGINE_NAME, 'general')], 'en-US', SAFESEARCH, PAGENO, None, 15.0
        )
        search = searx.search.Search(search_query)
        with self.app.test_request_context('/search'):
            search.search()
        self.assertEqual(search.actual_timeout, 10.0)

    def test_external_bang_valid(self):
        search_query = SearchQuery(
            'yes yes',
            [EngineRef(PUBLIC_ENGINE_NAME, 'general')],
            'en-US',
            SAFESEARCH,
            PAGENO,
            None,
            None,
            external_bang="yt",
        )
        search = searx.search.Search(search_query)
        results = search.search()
        # For checking if the user redirected with the youtube external bang
        self.assertIsNotNone(results.redirect_url)

    def test_external_bang_none(self):
        search_query = SearchQuery(
            'youtube never gonna give you up',
            [EngineRef(PUBLIC_ENGINE_NAME, 'general')],
            'en-US',
            SAFESEARCH,
            PAGENO,
            None,
            None,
        )

        search = searx.search.Search(search_query)
        with self.app.test_request_context('/search'):
            results = search.search()
        # This should not redirect
        self.assertIsNone(results.redirect_url)
