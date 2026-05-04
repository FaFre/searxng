# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring

import os
import tempfile
import unittest
from unittest.mock import Mock
from urllib.parse import urlparse

from searx.plugins.weblibre_goggles import (
    GoggleParseError,
    SXNGPlugin,
    _load_directory,
    parse_goggle,
)


class ParserTests(unittest.TestCase):
    def test_headers_parse(self):
        g = parse_goggle(
            "x",
            "! name: Tech\n! description: things\n! something else\n",
        )
        self.assertEqual(g.name, "Tech")
        self.assertEqual(g.description, "things")
        self.assertEqual(g.rules, [])
        self.assertFalse(g.default_discard)

    def test_bare_discard_flips_default(self):
        g = parse_goggle("x", "$discard\n$boost=2,site=github.com\n")
        self.assertTrue(g.default_discard)
        self.assertEqual(len(g.rules), 1)

    def test_strength_default_is_2(self):
        g = parse_goggle("x", "$boost,site=a.com\n$downrank,site=b.com\n")
        self.assertEqual(g.rules[0].strength, 2)
        self.assertEqual(g.rules[1].strength, 2)

    def test_strength_out_of_range_rejected(self):
        with self.assertRaises(GoggleParseError):
            parse_goggle("x", "$boost=11,site=a.com\n")
        with self.assertRaises(GoggleParseError):
            parse_goggle("x", "$boost=0,site=a.com\n")

    def test_unknown_option_rejected(self):
        with self.assertRaises(GoggleParseError):
            parse_goggle("x", "$frobnicate=2,site=a.com\n")

    def test_wildcard_anchor_caret_compile(self):
        g = parse_goggle("x", "/blog/$boost=1\n")
        rule = g.rules[0]
        assert rule.regex is not None
        self.assertTrue(rule.regex.search("https://x.example/blog/post"))
        self.assertFalse(rule.regex.search("https://x.example/news/post"))

        g2 = parse_goggle("x", "|https://example.com/*/end|$boost=1\n")
        r2 = g2.rules[0]
        assert r2.regex is not None
        self.assertTrue(r2.regex.search("https://example.com/foo/bar/end"))
        self.assertFalse(r2.regex.search("https://example.com/foo/end/extra"))

        g3 = parse_goggle("x", "/api^$boost=1\n")
        r3 = g3.rules[0]
        assert r3.regex is not None
        self.assertTrue(r3.regex.search("https://x.example/api/v1"))
        self.assertTrue(r3.regex.search("https://x.example/api?q=1"))
        self.assertFalse(r3.regex.search("https://x.example/apiary"))

    def test_too_many_wildcards(self):
        with self.assertRaises(GoggleParseError):
            parse_goggle("x", "*a*b*c$boost=1\n")

    def test_line_too_long(self):
        long_line = "/" + "a" * 250 + "$boost=1\n"
        with self.assertRaises(GoggleParseError):
            parse_goggle("x", long_line)


class _FakeResult:
    """Minimal stand-in for MainResult — has .url, .parsed_url, .engine
    and the score_multiplier channel the plugin writes to. The plugin's
    isinstance gate against MainResult/LegacyResult means we have to
    register this class as a virtual subclass per test path; instead, we
    monkey-patch the gate at call time in MatchingTests._run.
    """

    def __init__(self, url: str):
        self.url = url
        self.parsed_url = urlparse(url)
        self.engine = ""
        self.score_multiplier = 1.0


class MatchingTests(unittest.TestCase):
    """Validate Marginalia-shaped scoring through the plugin's on_result
    path. Boost adds to the bonus accumulator, downrank subtracts, and the
    final multiplier is exp(sum / softness). Discard returns False."""

    def _make_plugin(self, registry, softness=5.0):
        from searx.plugins.weblibre_goggles import DEFAULT_SCORE_SOFTNESS

        plugin = SXNGPlugin.__new__(SXNGPlugin)
        plugin.registry = registry
        plugin.score_softness = softness or DEFAULT_SCORE_SOFTNESS
        plugin.log = Mock()
        return plugin

    def _run(self, goggle, url):
        """Invoke plugin.on_result with the goggle attached. Returns
        (kept, score_multiplier) where ``kept`` is the on_result return
        value and ``score_multiplier`` reflects accumulated adjustments."""
        from unittest.mock import patch
        from searx.plugins import weblibre_goggles as mod

        plugin = self._make_plugin({"g": goggle})
        search = Mock()
        search.weblibre_goggles = [goggle]
        result = _FakeResult(url)
        # MainResult is a msgspec.Struct (no virtual-subclass registration),
        # so swap the names the plugin's isinstance gate looks at to point
        # at _FakeResult for the duration of the call.
        with patch.object(mod, "MainResult", _FakeResult), patch.object(
            mod, "LegacyResult", _FakeResult
        ):
            kept = plugin.on_result(Mock(), search, result)
        return kept, result.score_multiplier

    def test_site_matches_host_and_subdomain(self):
        g = parse_goggle("x", "$boost=2,site=github.com\n")
        kept, m = self._run(g, "https://github.com/foo")
        self.assertTrue(kept)
        self.assertGreater(m, 1.0)
        kept, m = self._run(g, "https://api.github.com/foo")
        self.assertTrue(kept)
        self.assertGreater(m, 1.0)
        # unrelated host that merely contains the substring → no match
        kept, m = self._run(g, "https://notgithub.com/foo")
        self.assertTrue(kept)
        self.assertEqual(m, 1.0)

    def test_discard_drops_result_even_with_boost(self):
        g = parse_goggle(
            "x", "$boost=5,site=example.com\n$discard,site=example.com\n"
        )
        kept, _ = self._run(g, "https://example.com/x")
        self.assertFalse(kept)

    def test_boost_and_downrank_are_additive(self):
        # boost=2 + downrank=2 → bonus_sum 0 → multiplier 1.0 (no change)
        g = parse_goggle(
            "x", "$downrank=2,site=example.com\n$boost=2,site=example.com\n"
        )
        kept, m = self._run(g, "https://example.com/x")
        self.assertTrue(kept)
        self.assertAlmostEqual(m, 1.0)

    def test_default_discard_drops_unmatched_keeps_boosted(self):
        g = parse_goggle("x", "$discard\n$boost=2,site=keep.com\n")
        kept_keep, m_keep = self._run(g, "https://keep.com/a")
        self.assertTrue(kept_keep)
        self.assertGreater(m_keep, 1.0)
        kept_other, _ = self._run(g, "https://other.com/a")
        self.assertFalse(kept_other)

    def test_two_boosts_accumulate(self):
        # boost=3 + boost=7 → bonus_sum 10 → exp(10/5) ≈ 7.39
        import math

        g = parse_goggle(
            "x", "$boost=3,site=a.com\n$boost=7,site=a.com\n"
        )
        kept, m = self._run(g, "https://a.com/x")
        self.assertTrue(kept)
        self.assertAlmostEqual(m, math.exp(10 / 5), places=4)

    def test_softness_curve(self):
        import math

        for strength in (1, 5, 10):
            g = parse_goggle("x", f"$boost={strength},site=a.com\n")
            _, m = self._run(g, "https://a.com/x")
            self.assertAlmostEqual(m, math.exp(strength / 5), places=4)

    def test_downrank_curve(self):
        import math

        g = parse_goggle("x", "$downrank=10,site=a.com\n")
        kept, m = self._run(g, "https://a.com/x")
        self.assertTrue(kept)
        self.assertAlmostEqual(m, math.exp(-10 / 5), places=4)


class OnResultLifecycleTests(unittest.TestCase):
    def _make_plugin(self, registry):
        plugin = SXNGPlugin.__new__(SXNGPlugin)
        plugin.registry = registry
        plugin.log = Mock()
        return plugin

    def test_on_result_returns_false_on_discard(self):
        g = parse_goggle("x", "$discard,site=bad.com\n")
        plugin = self._make_plugin({"g": g})
        search = Mock()
        search.weblibre_goggles = [g]
        result = _FakeResult("https://bad.com/x")
        ret = plugin.on_result(Mock(), search, result)
        self.assertFalse(ret)

    def test_on_result_keeps_unmatched(self):
        g = parse_goggle("x", "$boost=2,site=foo.com\n")
        plugin = self._make_plugin({"g": g})
        search = Mock()
        search.weblibre_goggles = [g]
        result = _FakeResult("https://other.com/x")
        ret = plugin.on_result(Mock(), search, result)
        self.assertTrue(ret)

    def test_on_result_no_goggle_attached_passes_through(self):
        plugin = self._make_plugin({})
        search = Mock()
        search.weblibre_goggles = []
        result = _FakeResult("https://anywhere.com/x")
        self.assertTrue(plugin.on_result(Mock(), search, result))

    def test_on_result_multi_goggle_union_discard_wins(self):
        # boost from one goggle, discard from another → discard wins.
        boost_g = parse_goggle("x", "$boost=5,site=foo.com\n")
        block_g = parse_goggle("x", "$discard,site=foo.com\n")
        plugin = self._make_plugin({"a": boost_g, "b": block_g})
        search = Mock()
        search.weblibre_goggles = [boost_g, block_g]
        result = _FakeResult("https://foo.com/x")
        self.assertFalse(plugin.on_result(Mock(), search, result))

    def test_on_result_multi_goggle_default_discard_any(self):
        # default-discard from one goggle drops anything not boosted by
        # *any* goggle in the set.
        deflt = parse_goggle("x", "$discard\n")
        boost_g = parse_goggle("x", "$boost=2,site=keep.com\n")
        plugin = self._make_plugin({"a": deflt, "b": boost_g})
        search = Mock()
        search.weblibre_goggles = [deflt, boost_g]
        # boosted by the second goggle → kept
        self.assertTrue(plugin.on_result(Mock(), search, _FakeResult("https://keep.com/x")))
        # not matched by either → discarded
        self.assertFalse(plugin.on_result(Mock(), search, _FakeResult("https://other.com/x")))

    def test_pre_search_resolves_single_goggle(self):
        g = parse_goggle("x", "$boost=2,site=foo.com\n")
        plugin = self._make_plugin({"my_goggle": g})
        request = Mock()
        request.form = {"goggle": "my_goggle"}
        search = Mock()
        plugin.pre_search(request, search)
        self.assertEqual(search.weblibre_goggles, [g])

    def test_pre_search_resolves_list(self):
        g1 = parse_goggle("x", "$boost=2,site=a.com\n")
        g2 = parse_goggle("x", "$boost=2,site=b.com\n")
        plugin = self._make_plugin({"one": g1, "two": g2})
        request = Mock()
        request.form = {"goggle": "one,two"}
        search = Mock()
        plugin.pre_search(request, search)
        self.assertEqual(search.weblibre_goggles, [g1, g2])

    def test_pre_search_drops_unknown_and_invalid(self):
        g = parse_goggle("x", "$boost=2,site=a.com\n")
        plugin = self._make_plugin({"good": g})
        request = Mock()
        request.form = {"goggle": "good,missing,../bad,good"}
        search = Mock()
        plugin.pre_search(request, search)
        # unknown + invalid skipped, duplicate de-duped
        self.assertEqual(search.weblibre_goggles, [g])

    def test_pre_search_caps_at_request_limit(self):
        from searx.plugins.weblibre_goggles import MAX_GOGGLES_PER_REQUEST

        registry = {
            f"g{i}": parse_goggle("x", f"$boost=2,site=h{i}.com\n")
            for i in range(MAX_GOGGLES_PER_REQUEST + 4)
        }
        plugin = self._make_plugin(registry)
        request = Mock()
        request.form = {"goggle": ",".join(registry.keys())}
        search = Mock()
        plugin.pre_search(request, search)
        self.assertEqual(len(search.weblibre_goggles), MAX_GOGGLES_PER_REQUEST)


class DirectoryLoaderTests(unittest.TestCase):
    def test_bad_file_skipped_others_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "good.goggle"), "w", encoding="utf-8") as f:
                f.write("$boost=2,site=good.com\n")
            with open(os.path.join(tmp, "bad.goggle"), "w", encoding="utf-8") as f:
                f.write("$frobnicate=2,site=bad.com\n")
            # filename violates the safe regex → silently skipped
            with open(os.path.join(tmp, "weird name.goggle"), "w", encoding="utf-8") as f:
                f.write("$boost=2,site=weird.com\n")

            registry = _load_directory(tmp)
            self.assertIn("good", registry)
            self.assertNotIn("bad", registry)

    def test_missing_directory_returns_empty(self):
        self.assertEqual(_load_directory("/nonexistent/path/xyz"), {})


if __name__ == "__main__":
    unittest.main()
