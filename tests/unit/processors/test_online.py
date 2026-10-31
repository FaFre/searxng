# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,disable=missing-class-docstring,invalid-name

import json
from unittest import mock

from searx.search.models import EngineRef, SearchQuery
from searx.search.processors import online
from searx import engines
from searx.extended_types import SXNG_Response

from tests import SearxTestCase

TEST_ENGINE_NAME = "dummy engine"  # from the ./settings/test_settings.yml


def _json_response(status_code, payload):
    response = SXNG_Response()
    response.status_code = status_code
    response.content = json.dumps(payload).encode()
    response.ok = 200 <= status_code < 400
    return response


class FakeResponseCache:
    def __init__(self, policy, response=None, stale_response=None, fresh_responses=None):
        self.policy = policy
        self.response = response
        # optional sequence of results for consecutive fresh lookups
        self.fresh_responses = list(fresh_responses) if fresh_responses is not None else None
        self.stale_response = stale_response
        self.set_calls = []
        self.release_calls = []
        self.get_calls = []

    def get_policy(self, engine):  # pylint: disable=unused-argument
        return self.policy

    def make_key(self, policy, params):  # pylint: disable=unused-argument
        return "cache-key"

    def get(self, key, params, fresh_only=True):  # pylint: disable=unused-argument
        self.get_calls.append(fresh_only)
        if fresh_only:
            if self.fresh_responses is not None:
                return self.fresh_responses.pop(0)
            return self.response
        return self.stale_response

    def set(self, key, policy, response):
        self.set_calls.append((key, policy, response))
        return True

    def delete(self, key):  # pylint: disable=unused-argument
        return True

    def should_fallback_to_stale(self, response):
        return response.status_code == 429

    def acquire_inflight(self, key, wait_timeout):  # pylint: disable=unused-argument
        return True

    def release_inflight(self, key):
        self.release_calls.append(key)


class TestOnlineProcessor(SearxTestCase):

    def set_engine_attr(self, engine, attr, value):
        had_attr = hasattr(engine, attr)
        previous_value = getattr(engine, attr, None)

        def cleanup_patch():
            if had_attr:
                setattr(engine, attr, previous_value)
            else:
                delattr(engine, attr)

        self.addCleanup(cleanup_patch)
        setattr(engine, attr, value)

    def _get_params(self, online_processor, search_query, engine_category):
        params = online_processor.get_params(search_query, engine_category)
        self.assertIsNotNone(params)
        assert params is not None
        return params

    def test_get_params_default_params(self):
        engine = engines.engines[TEST_ENGINE_NAME]
        online_processor = online.OnlineProcessor(engine)
        search_query = SearchQuery('test', [EngineRef(TEST_ENGINE_NAME, 'general')], 'all', 0, 1, None, None, None)
        params = self._get_params(online_processor, search_query, 'general')
        self.assertIn('method', params)
        self.assertIn('headers', params)
        self.assertIn('data', params)
        self.assertIn('url', params)
        self.assertIn('cookies', params)
        self.assertIn('auth', params)

    def test_get_params_useragent(self):
        engine = engines.engines[TEST_ENGINE_NAME]
        online_processor = online.OnlineProcessor(engine)
        search_query = SearchQuery('test', [EngineRef(TEST_ENGINE_NAME, 'general')], 'all', 0, 1, None, None, None)
        params = self._get_params(online_processor, search_query, 'general')
        self.assertNotIn('User-Agent', params['headers'])

    def test_search_basic_uses_fresh_cached_response(self):
        engine = engines.engines[TEST_ENGINE_NAME]
        online_processor = online.OnlineProcessor(engine)

        cached_response = _json_response(200, {"source": "cache"})

        response_cache = FakeResponseCache(policy=object(), response=cached_response)
        self.setattr4test(online, 'get_online_response_cache', lambda: response_cache)
        self.set_engine_attr(engine, 'request', lambda query, params: params.__setitem__('url', 'https://example.com/search?q=' + query))
        self.set_engine_attr(engine, 'response', lambda resp: resp.json()["source"])
        self.setattr4test(online_processor, '_send_http_request', mock.Mock(side_effect=AssertionError('live request should not run')))

        search_query = SearchQuery('test', [EngineRef(TEST_ENGINE_NAME, 'general')], 'all', 0, 1, None, None, None)
        params = self._get_params(online_processor, search_query, 'general')

        self.assertEqual(online_processor._search_basic('test', params), 'cache')
        self.assertEqual(response_cache.get_calls, [True])
        self.assertEqual(response_cache.set_calls, [])
        self.assertEqual(response_cache.release_calls, [])

    def test_search_basic_stores_successful_live_response(self):
        engine = engines.engines[TEST_ENGINE_NAME]
        online_processor = online.OnlineProcessor(engine)

        live_response = _json_response(200, {"source": "live"})

        response_cache = FakeResponseCache(policy=object())
        self.setattr4test(online, 'get_online_response_cache', lambda: response_cache)
        self.set_engine_attr(engine, 'request', lambda query, params: params.__setitem__('url', 'https://example.com/search?q=' + query))
        self.set_engine_attr(engine, 'response', lambda resp: resp.json()["source"])
        self.setattr4test(online_processor, '_send_http_request', mock.Mock(return_value=live_response))

        search_query = SearchQuery('test', [EngineRef(TEST_ENGINE_NAME, 'general')], 'all', 0, 1, None, None, None)
        params = self._get_params(online_processor, search_query, 'general')

        self.assertEqual(online_processor._search_basic('test', params), 'live')
        self.assertEqual(len(response_cache.set_calls), 1)
        self.assertEqual(response_cache.release_calls, ['cache-key'])

    def test_search_basic_uses_stale_cache_on_429(self):
        engine = engines.engines[TEST_ENGINE_NAME]
        online_processor = online.OnlineProcessor(engine)

        stale_response = _json_response(200, {"source": "stale"})
        live_response = _json_response(429, {"source": "live"})

        response_cache = FakeResponseCache(policy=object(), stale_response=stale_response)
        self.setattr4test(online, 'get_online_response_cache', lambda: response_cache)
        self.set_engine_attr(engine, 'request', lambda query, params: params.__setitem__('url', 'https://example.com/search?q=' + query))
        self.set_engine_attr(engine, 'response', lambda resp: resp.json()["source"])
        self.setattr4test(online_processor, '_send_http_request', mock.Mock(return_value=live_response))

        search_query = SearchQuery('test', [EngineRef(TEST_ENGINE_NAME, 'general')], 'all', 0, 1, None, None, None)
        params = self._get_params(online_processor, search_query, 'general')

        self.assertEqual(online_processor._search_basic('test', params), 'stale')
        # fresh lookup, fresh re-check after acquiring the in-flight slot, stale fallback
        self.assertEqual(response_cache.get_calls, [True, True, False])
        self.assertEqual(response_cache.set_calls, [])
        self.assertEqual(response_cache.release_calls, ['cache-key'])

    def test_search_basic_rechecks_cache_after_inflight_wait(self):
        """A concurrent request for the same key stored the response while this
        one waited for the in-flight slot: no second upstream request."""
        engine = engines.engines[TEST_ENGINE_NAME]
        online_processor = online.OnlineProcessor(engine)

        cached_response = _json_response(200, {"source": "cache"})

        response_cache = FakeResponseCache(policy=object(), fresh_responses=[None, cached_response])
        self.setattr4test(online, 'get_online_response_cache', lambda: response_cache)
        self.set_engine_attr(engine, 'request', lambda query, params: params.__setitem__('url', 'https://example.com/search?q=' + query))
        self.set_engine_attr(engine, 'response', lambda resp: resp.json()["source"])
        self.setattr4test(online_processor, '_send_http_request', mock.Mock(side_effect=AssertionError('live request should not run')))

        search_query = SearchQuery('test', [EngineRef(TEST_ENGINE_NAME, 'general')], 'all', 0, 1, None, None, None)
        params = self._get_params(online_processor, search_query, 'general')

        self.assertEqual(online_processor._search_basic('test', params), 'cache')
        self.assertEqual(response_cache.get_calls, [True, True])
        self.assertEqual(response_cache.set_calls, [])
        # the in-flight slot is released on the early return as well
        self.assertEqual(response_cache.release_calls, ['cache-key'])
