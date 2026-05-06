# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring

from searx import metrics

from tests import SearxTestCase


class TestMetricsOpenMetrics(SearxTestCase):
    def test_openmetrics_includes_response_cache_counters(self):
        engine_name = 'dummy engine'
        metrics.counter_inc('engine', engine_name, 'response_cache', 'count', 'hit')
        metrics.counter_inc('engine', engine_name, 'response_cache', 'count', 'miss')
        metrics.counter_inc('engine', engine_name, 'response_cache', 'count', 'stale_hit')

        engine_stats = {
            'time': [
                {
                    'name': engine_name,
                    'total': 0,
                    'processing': 0,
                    'http': 0,
                    'result_count': 0,
                }
            ]
        }
        engine_reliabilities = {engine_name: {'sent_count': 1, 'reliability': 100}}

        text = metrics.openmetrics(engine_stats, engine_reliabilities)

        self.assertIn('searxng_engines_response_cache_hits_total{engine_name="dummy engine"} 1', text)
        self.assertIn('searxng_engines_response_cache_misses_total{engine_name="dummy engine"} 1', text)
        self.assertIn('searxng_engines_response_cache_stale_hits_total{engine_name="dummy engine"} 1', text)
