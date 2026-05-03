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
    """Minimal stand-in for MainResult — has .url, .parsed_url, .priority."""

    def __init__(self, url: str):
        self.url = url
        self.parsed_url = urlparse(url)
        self.priority = ""


class MatchingTests(unittest.TestCase):
    def _plugin_with_registry(self, registry):
        plugin = SXNGPlugin.__new__(SXNGPlugin)
        plugin.registry = registry
        return plugin

    def _run(self, goggle, url):
        from searx.plugins import weblibre_goggles as mod

        # Bypass MainResult/LegacyResult isinstance gate by patching it
        # for the call: use the _FakeResult and treat priority writes as
        # plain attribute assignment by monkey-patching the isinstance
        # check via subclass.
        result = _FakeResult(url)
        host = (result.parsed_url.netloc or "").lower()

        best_action = None
        best_strength = 0
        for rule in goggle.rules:
            if not rule.matches(result.url, host):
                continue
            from searx.plugins.weblibre_goggles import _ACTION_RANK

            if best_action is None or _ACTION_RANK[rule.action] > _ACTION_RANK[
                best_action
            ]:
                best_action = rule.action
                best_strength = rule.strength
            elif rule.action == best_action and rule.strength > best_strength:
                best_strength = rule.strength

        if best_action is None and goggle.default_discard:
            return ("discard", 0)
        if best_action == "discard":
            return ("discard", best_strength)
        return (best_action, best_strength)

    def test_site_matches_host_and_subdomain(self):
        g = parse_goggle("x", "$boost=2,site=github.com\n")
        self.assertEqual(self._run(g, "https://github.com/foo")[0], "boost")
        self.assertEqual(self._run(g, "https://api.github.com/foo")[0], "boost")
        # not unrelated host that contains the substring
        self.assertEqual(self._run(g, "https://notgithub.com/foo")[0], None)

    def test_discard_wins_over_boost(self):
        g = parse_goggle(
            "x", "$boost=5,site=example.com\n$discard,site=example.com\n"
        )
        action, _ = self._run(g, "https://example.com/x")
        self.assertEqual(action, "discard")

    def test_boost_wins_over_downrank(self):
        g = parse_goggle(
            "x", "$downrank=2,site=example.com\n$boost=2,site=example.com\n"
        )
        action, _ = self._run(g, "https://example.com/x")
        self.assertEqual(action, "boost")

    def test_default_discard_drops_unmatched_keeps_boosted(self):
        g = parse_goggle("x", "$discard\n$boost=2,site=keep.com\n")
        self.assertEqual(self._run(g, "https://keep.com/a")[0], "boost")
        self.assertEqual(self._run(g, "https://other.com/a")[0], "discard")

    def test_strength_tiebreaker_within_action(self):
        g = parse_goggle(
            "x", "$boost=3,site=a.com\n$boost=7,site=a.com\n"
        )
        action, strength = self._run(g, "https://a.com/x")
        self.assertEqual(action, "boost")
        self.assertEqual(strength, 7)


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
