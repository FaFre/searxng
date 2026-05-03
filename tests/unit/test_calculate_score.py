# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,invalid-name

import types
import unittest
from unittest.mock import patch

from searx.results import calculate_score


def _engine(weight=None):
    """Mock engine module — has a .weight attr only if one was supplied."""
    engine = types.SimpleNamespace()
    if weight is not None:
        engine.weight = weight
    return engine


class CalculateScoreTestCase(unittest.TestCase):
    """Exercise the real ``searx.results.calculate_score`` function with
    ``searx.engines.engines`` patched to a controlled registry."""

    def _result(self, engines, positions, priority=''):
        return {
            'engines': set(engines),
            'positions': positions,
            'priority': priority,
        }

    def _score(self, result, weight_overrides=None, registry=None):
        registry = registry or {}
        with patch.dict('searx.engines.engines', registry, clear=True):
            return calculate_score(result, result['priority'], weight_overrides)

    def test_no_override_default_weight(self):
        """Engine without a weight attr defaults to 1."""
        result = self._result(['google'], [1])
        self.assertEqual(self._score(result, registry={'google': _engine()}), 1.0)

    def test_no_override_yaml_weight(self):
        """YAML weight is applied when no override is given."""
        result = self._result(['google'], [1])
        self.assertEqual(self._score(result, registry={'google': _engine(2.0)}), 2.0)

    def test_override_replaces_yaml_weight(self):
        """Override fully replaces the YAML weight (no multiplication)."""
        result = self._result(['google'], [1])
        score = self._score(
            result,
            weight_overrides={'google': 3.0},
            registry={'google': _engine(2.0)},
        )
        self.assertEqual(score, 3.0)

    def test_soft_disable_only_engine(self):
        """weight=0 on the only engine sinks the result to score 0."""
        result = self._result(['google'], [1])
        score = self._score(
            result,
            weight_overrides={'google': 0.0},
            registry={'google': _engine(2.0)},
        )
        self.assertEqual(score, 0)

    def test_soft_disable_one_of_two_engines(self):
        """A soft-disabled engine drops out; the other contributes normally."""
        result = self._result(['google', 'duckduckgo'], [1, 3])
        score = self._score(
            result,
            weight_overrides={'google': 0.0},
            registry={'google': _engine(5.0), 'duckduckgo': _engine(1.0)},
        )
        # google skipped; duckduckgo weight=1, positions [1,3] → 1*2*(1/1+1/3)
        self.assertAlmostEqual(score, 1.0 * 2 * (1.0 / 1 + 1.0 / 3))

    def test_soft_disable_all_engines(self):
        """All contributing engines disabled → score 0."""
        result = self._result(['google', 'duckduckgo'], [1])
        score = self._score(
            result,
            weight_overrides={'google': 0.0, 'duckduckgo': 0.0},
            registry={'google': _engine(2.0), 'duckduckgo': _engine(3.0)},
        )
        self.assertEqual(score, 0)

    def test_no_weight_attribute_contributes_normally(self):
        """Engines without ``weight`` attr default to 1 and still contribute."""
        result = self._result(['google'], [1])
        self.assertEqual(self._score(result, registry={'google': _engine()}), 1.0)

    def test_mixed_override_and_default(self):
        """Override on one engine, default weight on another."""
        result = self._result(['google', 'brave'], [1])
        score = self._score(
            result,
            weight_overrides={'google': 2.0},
            registry={'google': _engine(), 'brave': _engine()},
        )
        # weight = 2.0 * 1.0 = 2.0, positions=[1] → score = 2.0/1
        self.assertEqual(score, 2.0)

    def test_override_on_engine_without_yaml_weight(self):
        result = self._result(['google'], [1])
        score = self._score(
            result,
            weight_overrides={'google': 5.0},
            registry={'google': _engine()},
        )
        self.assertEqual(score, 5.0)

    def test_low_priority_with_override(self):
        """Low priority short-circuits the per-position contribution."""
        result = self._result(['google'], [1], priority='low')
        score = self._score(
            result,
            weight_overrides={'google': 10.0},
            registry={'google': _engine()},
        )
        self.assertEqual(score, 0)

    def test_high_priority_with_override(self):
        """High priority uses flat weight per position."""
        result = self._result(['google'], [1, 2], priority='high')
        score = self._score(
            result,
            weight_overrides={'google': 3.0},
            registry={'google': _engine()},
        )
        # weight = 3.0 * len(positions=2) = 6.0; score = 6.0 + 6.0 = 12.0
        self.assertEqual(score, 12.0)

    def test_engine_missing_from_registry(self):
        """Engine present on result but absent from registry: default weight=1."""
        result = self._result(['ghost'], [1])
        self.assertEqual(self._score(result, registry={}), 1.0)


if __name__ == '__main__':
    unittest.main()
