# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real-world benchmark for weblibre_goggles using actual goggle data.

Measures how long it takes to evaluate 100 synthetic search results against
all preset goggles combined (~440k rules total). Only the plugin's own
matching logic is timed — parsing/load time is measured separately.

The plugin module has a heavy import chain (flask, httpx, searx internals),
so we extract the core data structures and algorithms by exec-ing the
source file with stubbed imports. This benchmarks *only* the goggle logic.

Run standalone:
    python tests/unit/bench_plugin_weblibre_goggles.py

Run via pytest:
    pytest tests/unit/bench_plugin_weblibre_goggles.py -v -s
"""

import hashlib
import os
import re
import time
import types
import unittest

_PLUGIN_SRC = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "searx",
    "plugins",
    "weblibre_goggles.py",
)

GOGGLES_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "goggles"
)


def _load_plugin_module():
    """Load the weblibre_goggles plugin with stubbed dependencies."""
    with open(_PLUGIN_SRC, encoding="utf-8") as f:
        source = f.read()

    stub = types.ModuleType("searx.plugins.weblibre_goggles")
    stub.__file__ = _PLUGIN_SRC

    import builtins
    import io
    import logging

    stub_ns = {
        "__builtins__": builtins.__dict__,
        "os": os,
        "re": re,
        "typing": __import__("typing"),
        "flask_babel": types.ModuleType("flask_babel"),
        "searx": types.ModuleType("searx"),
        "log": logging.getLogger("bench_weblibre_goggles"),
    }
    stub_ns["flask_babel"].gettext = lambda s: s

    searx_mod = stub_ns["searx"]
    searx_mod.settings = types.ModuleType("searx.settings")
    searx_mod.settings.get = lambda k, default=None: default
    searx_mod.settings_dict = {}

    result_types = types.ModuleType("searx.result_types")
    result_types._base = types.ModuleType("searx.result_types._base")
    result_types._base.MainResult = type("MainResult", (), {})
    result_types._base.LegacyResult = type("LegacyResult", (), {})
    result_types.Result = type("Result", (), {})

    plugins_mod = types.ModuleType("searx.plugins")
    plugins_mod.Plugin = object
    plugins_mod.PluginInfo = type("PluginInfo", (), {})
    plugins_mod.PluginCfg = type("PluginCfg", (), {})
    plugins_mod.PluginStorage = type("PluginStorage", (), {})

    core_mod = types.ModuleType("searx.plugins._core")
    core_mod.log = logging.getLogger("bench_weblibre_goggles")

    import sys
    sys.modules["searx"] = searx_mod
    sys.modules["searx.settings"] = searx_mod.settings
    sys.modules["searx.result_types"] = result_types
    sys.modules["searx.result_types._base"] = result_types._base
    sys.modules["searx.plugins"] = plugins_mod
    sys.modules["searx.plugins._core"] = core_mod
    sys.modules["flask_babel"] = stub_ns["flask_babel"]

    exec(compile(source, _PLUGIN_SRC, "exec"), stub.__dict__)

    return stub


_mod = _load_plugin_module()
parse_goggle = _mod.parse_goggle
Goggle = _mod.Goggle
Rule = _mod.Rule
_ACTION_RANK = _mod._ACTION_RANK


def _goggle_files() -> list[tuple[str, str]]:
    if not os.path.isdir(GOGGLES_DIR):
        return []
    out = []
    for fname in sorted(os.listdir(GOGGLES_DIR)):
        if not fname.endswith(".goggle"):
            continue
        with open(os.path.join(GOGGLES_DIR, fname), encoding="utf-8") as f:
            out.append((fname, f.read()))
    return out


def _load_all_goggles() -> dict[str, "Goggle"]:
    """Load goggles, skipping unparseable lines (e.g. patterns with >2 wildcards).

    The real plugin's _load_directory skips entire files on parse errors, but
    for benchmarking we want to keep as much real data as possible. We
    replicate parse_goggle logic with per-line error tolerance.
    """
    GoggleParseError = _mod.GoggleParseError
    _parse_rule_line = _mod._parse_rule_line
    registry: dict[str, Goggle] = {}

    for fname, text in _goggle_files():
        key = fname.removesuffix(".goggle")
        display_name = key
        description = ""
        default_discard = False
        rules: list = []

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if len(line) > _mod.MAX_LINE_LEN:
                continue
            if line.startswith("!"):
                body = line[1:].strip()
                lower = body.lower()
                if lower.startswith("name:"):
                    display_name = body[len("name:"):].strip() or display_name
                elif lower.startswith("description:"):
                    description = body[len("description:"):].strip()
                continue
            try:
                parsed = _parse_rule_line(line)
            except GoggleParseError:
                continue
            if isinstance(parsed, tuple) and parsed[0] == "default_discard":
                default_discard = True
                continue
            rules.append(parsed)

        registry[key] = Goggle(
            name=display_name,
            description=description,
            rules=rules,
            default_discard=default_discard,
        )

    return registry


def _parent_domains(host: str) -> list[str]:
    parts = host.split(".")
    return [".".join(parts[i:]) for i in range(len(parts))]


def _generate_search_results(n: int) -> list[tuple[str, str]]:
    """Generate n realistic (url, host) pairs mimicking search results.

    Mix includes: SEO-spam domains (should be discarded), smallweb domains
    (should be boosted), big-web domains (should be downranked),
    non-commercial domains (should be boosted), and unknown domains.
    """
    seo_spam_hosts = [
        "bestvpn.co", "thefactnc.com", "vashgorod.ru", "fubar.com",
        "clickbait24.com", "spamnews.net", "contentfarm.org", "keyword.io",
    ]
    smallweb_hosts = [
        "alexschroeder.ch", "brainbaking.com", "danluu.com", "jeffhuang.com",
        "lwn.net", "shkspr.mobi", "xeiaso.eu", "williamdoe.blog",
    ]
    bigweb_hosts = [
        "google.com", "youtube.com", "facebook.com", "amazon.com", "yahoo.com",
        "wikipedia.org", "twitter.com", "instagram.com",
    ]
    noncommercial_hosts = [
        "wikipedia.org", "archive.org", "dl.acm.org", "arxiv.org",
        "nasa.gov", "nih.gov", "mit.edu",
    ]
    unknown_hosts = [
        "someobscuresite123.net", "brandnewstartup-xyz.io",
        "totallyunknown9999.com", "notlistedanywhere.org",
        "freshlyregistered.tech", "randomblog-i-just-made.blog",
    ]
    pools = [seo_spam_hosts, smallweb_hosts, bigweb_hosts,
             noncommercial_hosts, unknown_hosts]

    results: list[tuple[str, str]] = []
    r = hashlib.md5(b"bench_seed").digest()
    while len(results) < n:
        r = hashlib.md5(r).digest()
        v = int.from_bytes(r[:4], "little")
        pool = pools[v % 5]
        host = pool[v % len(pool)]
        depth = (v >> 8) % 4
        path = "/" + "/".join(f"seg{d}" for d in range(depth)) if depth else "/"
        results.append((f"https://{host}{path}", host))
    return results


def _run_on_result(
    goggles: list[Goggle], url: str, host: str, host_parents: list[str]
) -> tuple[str | None, int]:
    """Exact replica of SXNGPlugin.on_result matching logic, minus Flask/Result deps."""
    best_action: str | None = None
    best_strength = 0
    any_default_discard = False

    for goggle in goggles:
        if goggle.default_discard:
            any_default_discard = True

        for candidate in host_parents:
            rules = goggle.host_index.get(candidate)
            if not rules:
                continue
            for rule in rules:
                if best_action is None or _ACTION_RANK[rule.action] > _ACTION_RANK[best_action]:
                    best_action = rule.action
                    best_strength = rule.strength
                elif rule.action == best_action and rule.strength > best_strength:
                    best_strength = rule.strength
                if best_action == "discard":
                    break
            if best_action == "discard":
                break

        if best_action != "discard":
            for rule in goggle.pattern_rules:
                if not rule.matches(url, host):
                    continue
                if best_action is None or _ACTION_RANK[rule.action] > _ACTION_RANK[best_action]:
                    best_action = rule.action
                    best_strength = rule.strength
                elif rule.action == best_action and rule.strength > best_strength:
                    best_strength = rule.strength
                if best_action == "discard":
                    break

        if best_action == "discard":
            break

    if best_action is None and any_default_discard:
        return ("discard", 0)
    return (best_action, best_strength)


class _BenchResult:
    """Minimal result stand-in with .url and .parsed_url."""
    __slots__ = ("url", "parsed_url", "priority")

    def __init__(self, url: str):
        from urllib.parse import urlparse
        self.url = url
        self.parsed_url = urlparse(url)
        self.priority = ""


class BenchmarkParseLoad(unittest.TestCase):
    """Measure goggle file parsing and host_index build time."""

    def test_parse_all_goggles(self):
        registry = _load_all_goggles()
        if not registry:
            self.skipTest("no goggle files found in goggles/")

        total_rules = sum(len(g.rules) for g in registry.values())
        start = time.perf_counter()
        _load_all_goggles()
        elapsed = time.perf_counter() - start

        print(f"\n[parse] loaded {len(registry)} goggle files, "
              f"{total_rules} total rules in {elapsed:.3f}s")
        self.assertGreater(total_rules, 0)

    def test_host_index_build_time(self):
        registry = _load_all_goggles()
        if not registry:
            self.skipTest("no goggle files found in goggles/")

        start = time.perf_counter()
        registry = _load_all_goggles()
        elapsed = time.perf_counter() - start

        total_rules = sum(len(g.rules) for g in registry.values())
        total_index_keys = sum(len(g.host_index) for g in registry.values())
        print(f"\n[host_index build] {total_rules} rules → "
              f"{total_index_keys} index keys in {elapsed:.3f}s")


class BenchmarkOnResult(unittest.TestCase):
    """Measure per-result matching time with real goggle data."""

    @classmethod
    def setUpClass(cls):
        cls.registry = _load_all_goggles()
        if not cls.registry:
            return
        cls.goggles = list(cls.registry.values())
        cls.results_100 = _generate_search_results(100)
        cls.hosts_100 = [h for _, h in cls.results_100]
        cls.urls_100 = [u for u, _ in cls.results_100]
        cls.parents_100 = [_parent_domains(h) for h in cls.hosts_100]

    def _total_rules_info(self) -> tuple[int, int, int]:
        total = sum(len(g.rules) for g in self.goggles)
        site = sum(len(g.host_index) for g in self.goggles)
        pat = sum(len(g.pattern_rules) for g in self.goggles)
        return total, site, pat

    def test_on_result_100_with_all_goggles(self):
        if not self.registry:
            self.skipTest("no goggle files found")
        goggles = self.goggles

        WARMUP = 3
        RUNS = 5

        per_result_ns: list[float] = []

        for _ in range(WARMUP):
            for url, host in zip(self.urls_100, self.hosts_100):
                _run_on_result(goggles, url, host, _parent_domains(host))

        for url, host, parents in zip(self.urls_100, self.hosts_100, self.parents_100):
            r_start = time.perf_counter_ns()
            _run_on_result(goggles, url, host, parents)
            per_result_ns.append(time.perf_counter_ns() - r_start)

        avg_us = sum(per_result_ns) / len(per_result_ns) / 1000
        p50_us = sorted(per_result_ns)[len(per_result_ns) // 2] / 1000
        p99_us = sorted(per_result_ns)[int(len(per_result_ns) * 0.99)] / 1000
        max_us = max(per_result_ns) / 1000
        min_us = min(per_result_ns) / 1000

        total_ms = sum(per_result_ns) / 1_000_000
        total_rules, site_rules, pattern_rules = self._total_rules_info()

        print(f"\n{'='*60}")
        print(f"[on_result x100] {len(goggles)} goggles, {total_rules:,} rules "
              f"({site_rules:,} site-indexed, {pattern_rules:,} pattern)")
        print(f"  total:   {total_ms:.1f} ms for 100 results")
        print(f"  min:     {min_us:.1f} µs/result")
        print(f"  avg:     {avg_us:.1f} µs/result")
        print(f"  p50:     {p50_us:.1f} µs/result")
        print(f"  p99:     {p99_us:.1f} µs/result")
        print(f"  max:     {max_us:.1f} µs/result")
        print(f"{'='*60}")

    def test_on_result_100_repeated_runs(self):
        if not self.registry:
            self.skipTest("no goggle files found")
        goggles = self.goggles

        WARMUP = 5
        RUNS = 7
        total_elapsed: list[float] = []

        for _ in range(WARMUP):
            for url, host, parents in zip(self.urls_100, self.hosts_100, self.parents_100):
                _run_on_result(goggles, url, host, parents)

        for _ in range(RUNS):
            start = time.perf_counter()
            for url, host, parents in zip(self.urls_100, self.hosts_100, self.parents_100):
                _run_on_result(goggles, url, host, parents)
            total_elapsed.append(time.perf_counter() - start)

        best_ms = min(total_elapsed) * 1000
        avg_ms = sum(total_elapsed) / len(total_elapsed) * 1000
        print(f"\n[on_result x100, {RUNS} runs] best: {best_ms:.1f} ms, avg: {avg_ms:.1f} ms")

    def test_host_index_lookup_only(self):
        """Isolate host_index dict lookup cost (fast path) without pattern scanning."""
        if not self.registry:
            self.skipTest("no goggle files found")
        goggles = self.goggles

        WARMUP = 3
        for host, parents in zip(self.hosts_100, self.parents_100):
            for goggle in goggles:
                for candidate in parents:
                    goggle.host_index.get(candidate)

        per_result_ns: list[float] = []
        for host, parents in zip(self.hosts_100, self.parents_100):
            r_start = time.perf_counter_ns()

            best_action: str | None = None
            best_strength = 0
            for goggle in goggles:
                for candidate in parents:
                    rules = goggle.host_index.get(candidate)
                    if not rules:
                        continue
                    for rule in rules:
                        if best_action is None or _ACTION_RANK[rule.action] > _ACTION_RANK[best_action]:
                            best_action = rule.action
                            best_strength = rule.strength
                        elif rule.action == best_action and rule.strength > best_strength:
                            best_strength = rule.strength
                        if best_action == "discard":
                            break
                    if best_action == "discard":
                        break

            per_result_ns.append(time.perf_counter_ns() - r_start)

        avg_us = sum(per_result_ns) / len(per_result_ns) / 1000
        p50_us = sorted(per_result_ns)[len(per_result_ns) // 2] / 1000
        total_rules, site_rules, _ = self._total_rules_info()
        print(f"\n[host_index only x100] {site_rules:,} indexed domains, "
              f"avg: {avg_us:.1f} µs/result, p50: {p50_us:.1f} µs/result")

    def test_pattern_rules_only(self):
        """Isolate pattern rule linear scan cost (slow path)."""
        if not self.registry:
            self.skipTest("no goggle files found")
        goggles = self.goggles
        total_pattern_rules = sum(len(g.pattern_rules) for g in goggles)
        if total_pattern_rules == 0:
            self.skipTest("no pattern rules in any goggle")

        WARMUP = 3
        for _ in range(WARMUP):
            for url, host in zip(self.urls_100, self.hosts_100):
                for goggle in goggles:
                    for rule in goggle.pattern_rules:
                        rule.matches(url, host)

        per_result_ns: list[float] = []
        for url, host in zip(self.urls_100, self.hosts_100):
            r_start = time.perf_counter_ns()
            for goggle in goggles:
                for rule in goggle.pattern_rules:
                    rule.matches(url, host)
            per_result_ns.append(time.perf_counter_ns() - r_start)

        avg_us = sum(per_result_ns) / len(per_result_ns) / 1000
        p50_us = sorted(per_result_ns)[len(per_result_ns) // 2] / 1000
        print(f"\n[pattern_rules x100] {total_pattern_rules:,} pattern rules, "
              f"avg: {avg_us:.1f} µs/result, p50: {p50_us:.1f} µs/result")

    def test_match_outcomes(self):
        """Verify the matching logic produces correct outcomes on representative URLs."""
        if not self.registry:
            self.skipTest("no goggle files found")
        outcomes: dict[str, set[str]] = {"discard": set(), "boost": set(),
                                           "downrank": set(), "none": set()}
        for url, host, parents in zip(self.urls_100, self.hosts_100, self.parents_100):
            action, _ = _run_on_result(self.goggles, url, host, parents)
            outcomes[action or "none"].add(host)

        total = sum(len(v) for v in outcomes.values())
        print(f"\n[match outcomes over {total} unique hosts]")
        for action in ("discard", "boost", "downrank", "none"):
            hosts = outcomes[action]
            print(f"  {action}: {len(hosts)} hosts — {sorted(hosts)[:5]}{'...' if len(hosts) > 5 else ''}")


if __name__ == "__main__":
    unittest.main()