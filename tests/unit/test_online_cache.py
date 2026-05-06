# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring

from types import SimpleNamespace
from unittest import mock

from searx.search.online_cache import OnlineResponseCache, ResponseCachePolicy

from tests import SearxTestCase


class TestOnlineResponseCache(SearxTestCase):
    def test_get_policy_without_config_does_not_initialize_backend(self):
        with mock.patch(
            'searx.search.online_cache.valkeydb.client', side_effect=AssertionError('backend initialized')
        ):
            cache = OnlineResponseCache()

            self.assertIsNone(cache.get_policy(SimpleNamespace(name='example')))

    def test_get_policy_from_engine_config(self):
        cache = OnlineResponseCache()
        engine = SimpleNamespace(
            name='braveapi.news',
            response_cache={
                'enabled': True,
                'scope': 'braveapi',
                'ttl': 600,
                'stale_ttl': 3600,
                'max_body_size': 2048,
            },
        )

        policy = cache.get_policy(engine)

        self.assertEqual(
            policy,
            ResponseCachePolicy(enabled=True, scope='braveapi', ttl=600, stale_ttl=3600, max_body_size=2048),
        )

    def test_make_key_ignores_user_agent_but_varies_on_request_shape(self):
        cache = OnlineResponseCache()
        policy = ResponseCachePolicy(enabled=True, scope='mojeekapi', ttl=600, stale_ttl=600, max_body_size=1024)

        params_a = {
            'method': 'GET',
            'url': 'https://example.com/search?q=test&page=1',
            'headers': {'User-Agent': 'UA-A', 'Accept-Language': 'en-US,en;q=0.7'},
            'cookies': {},
            'auth': None,
            'data': {},
            'json': {},
            'content': b'',
        }
        params_b = {
            **params_a,
            'headers': {'User-Agent': 'UA-B', 'Accept-Language': 'en-US,en;q=0.7'},
        }
        params_c = {
            **params_a,
            'url': 'https://example.com/search?q=test&page=2',
        }

        self.assertEqual(cache.make_key(policy, params_a), cache.make_key(policy, params_b))
        self.assertNotEqual(cache.make_key(policy, params_a), cache.make_key(policy, params_c))
