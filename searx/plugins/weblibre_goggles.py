# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-class-docstring, unused-argument
"""Per-query Brave-Goggles-compatible ranking profiles.

Operators drop ``*.goggle`` files into a directory (configured via the
top-level ``goggles:`` settings block). Clients pick one per query by
sending a ``goggle=<name>`` form field. Matching rules can ``$discard``,
``$boost`` or ``$downrank`` results; under the hood we map those to
SearXNG's ``priority`` field (``high``/``low``) which is honored by
``searx.results.calculate_score``.

Supported subset of the Brave Goggles DSL:

- Patterns: literal substring, ``*`` wildcard, ``^`` URL delimiter,
  ``|``-prefix/suffix anchors.
- Options: ``$site=<host>``, ``$boost[=N]``, ``$downrank[=N]``,
  ``$discard``. Strength ``N`` is 1..10 (default 2).
- A bare ``$discard`` line (no pattern, no ``$site=``) flips the
  goggle's default action to discard.
- Header lines starting with ``!``: ``name:`` and ``description:`` are
  recognized; everything else is treated as a comment.

Known limitations vs. Brave Goggles: ``$inurl``, ``$intitle``,
``$indescription``, ``$incontent`` are NOT implemented — patterns match
the URL string only.
"""

import math
import os
import re
import typing as t

from flask_babel import gettext  # pyright: ignore[reportUnknownVariableType]

from searx import settings
from searx.result_types._base import MainResult, LegacyResult
from searx.plugins import Plugin, PluginInfo

from ._core import log

if t.TYPE_CHECKING:
    import flask
    from searx.extended_types import SXNG_Request
    from searx.search import SearchWithPlugins
    from searx.result_types import Result
    from searx.plugins import PluginCfg


_FILENAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}\.goggle$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

MAX_LINE_LEN = 200
# Uncapped: `$site=`-only rules are hashed (see Goggle.host_index) so
# per-result match cost is O(1) regardless of rule count.  Patterned
# rules scan linearly — keep those few and this limit need not apply.
MAX_RULES = 0
MAX_WILDCARDS = 2
MAX_DELIMS = 2
MAX_GOGGLES = 256
MAX_GOGGLES_PER_REQUEST = 8

# Marginalia-shaped scoring: each matching rule contributes ±strength to a
# per-result accumulator; the final score multiplier is exp(sum / softness).
# Default softness=5 mirrors Marginalia's `exp(priorityTermAdjustment / 5)`.
DEFAULT_SCORE_SOFTNESS = 5.0
# Clamp the per-result multiplier (mirrors `searx.results.calculate_score`'s
# clamp) so a runaway accumulator can't produce ±inf via exp().
MIN_SCORE_MULTIPLIER = 1e-3
MAX_SCORE_MULTIPLIER = 1e3


class GoggleParseError(ValueError):
    pass


class Rule:
    __slots__ = ("pattern_src", "regex", "site", "action", "strength")

    def __init__(
        self,
        pattern_src: str,
        regex: re.Pattern | None,
        site: str | None,
        action: str,
        strength: int,
    ) -> None:
        self.pattern_src = pattern_src
        self.regex = regex
        self.site = site
        self.action = action
        self.strength = strength

    def matches(self, url: str, host: str) -> bool:
        if self.site is not None:
            if host != self.site and not host.endswith("." + self.site):
                return False
        if self.regex is not None and not self.regex.search(url):
            return False
        return True


class Goggle:
    __slots__ = (
        "name",
        "description",
        "rules",
        "default_discard",
        "host_index",
        "pattern_rules",
        "hosted_url",
    )

    def __init__(
        self,
        name: str,
        description: str,
        rules: list[Rule],
        default_discard: bool,
        hosted_url: str | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.rules = rules
        self.default_discard = default_discard
        self.hosted_url = hosted_url
        # Fast path for `$site=`-only rules (no regex pattern): index by
        # site value so per-result lookup walks the host's parent domains
        # in O(host_depth) instead of scanning every rule. This is what
        # lets us carry 35k+ entries (e.g. Kagi smallweb) without paying
        # a linear scan per result.
        host_index: dict[str, list[Rule]] = {}
        pattern_rules: list[Rule] = []
        for r in rules:
            if r.site is not None and r.regex is None:
                host_index.setdefault(r.site, []).append(r)
            else:
                pattern_rules.append(r)
        self.host_index = host_index
        self.pattern_rules = pattern_rules


def _compile_pattern(pat: str) -> re.Pattern:
    """Compile a Brave-Goggles-style pattern to a regex against the
    full URL string."""

    if pat.count("*") > MAX_WILDCARDS:
        raise GoggleParseError(f"too many '*' in pattern: {pat!r}")
    if pat.count("^") > MAX_DELIMS:
        raise GoggleParseError(f"too many '^' in pattern: {pat!r}")

    anchor_start = pat.startswith("|")
    anchor_end = pat.endswith("|") and not (len(pat) >= 2 and pat[-2] == "\\")
    body = pat
    if anchor_start:
        body = body[1:]
    if anchor_end:
        body = body[:-1]

    out: list[str] = []
    for ch in body:
        if ch == "*":
            out.append(".*")
        elif ch == "^":
            out.append(r"(?:[/?#:&=]|$)")
        else:
            out.append(re.escape(ch))

    regex = "".join(out)
    if anchor_start:
        regex = "^" + regex
    if anchor_end:
        regex = regex + "$"
    return re.compile(regex)


def _parse_strength(raw: str | None, action: str) -> int:
    if raw is None or raw == "":
        return 2
    try:
        n = int(raw)
    except ValueError as exc:
        raise GoggleParseError(f"invalid {action} strength: {raw!r}") from exc
    if not 1 <= n <= 10:
        raise GoggleParseError(f"{action} strength out of range 1..10: {n}")
    return n


def _parse_rule_line(line: str) -> Rule | tuple[t.Literal["default_discard"]]:
    """Returns a Rule or the sentinel ``("default_discard",)`` for a
    bare ``$discard`` line."""

    if "$" in line:
        pat_part, _, opt_part = line.partition("$")
    else:
        pat_part, opt_part = line, ""

    site: str | None = None
    action: str | None = None
    strength = 2

    if opt_part:
        for opt in opt_part.split(","):
            opt = opt.strip()
            if not opt:
                continue
            if opt.startswith("site="):
                site = opt[len("site=") :].strip().lower()
                if not site:
                    raise GoggleParseError(f"empty site= in {line!r}")
            elif opt == "discard":
                if action is not None:
                    raise GoggleParseError(f"multiple actions in {line!r}")
                action = "discard"
            elif opt == "boost" or opt.startswith("boost="):
                if action is not None:
                    raise GoggleParseError(f"multiple actions in {line!r}")
                action = "boost"
                strength = _parse_strength(
                    opt[len("boost=") :] if "=" in opt else None, "boost"
                )
            elif opt == "downrank" or opt.startswith("downrank="):
                if action is not None:
                    raise GoggleParseError(f"multiple actions in {line!r}")
                action = "downrank"
                strength = _parse_strength(
                    opt[len("downrank=") :] if "=" in opt else None, "downrank"
                )
            else:
                raise GoggleParseError(f"unknown option {opt!r} in {line!r}")

    if action is None:
        action = "boost"  # bare pattern with no action → noop, but be permissive

    pat_part = pat_part.strip()

    # Bare $discard with no pattern and no site= flips default.
    if action == "discard" and not pat_part and site is None:
        return ("default_discard",)

    regex = _compile_pattern(pat_part) if pat_part else None
    if regex is None and site is None:
        raise GoggleParseError(f"rule has neither pattern nor site=: {line!r}")

    return Rule(
        pattern_src=pat_part,
        regex=regex,
        site=site,
        action=action,
        strength=strength,
    )


def parse_goggle(name: str, text: str) -> Goggle:
    description = ""
    display_name = name
    rules: list[Rule] = []
    default_discard = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if len(line) > MAX_LINE_LEN:
            raise GoggleParseError(f"line too long ({len(line)} > {MAX_LINE_LEN})")
        if line.startswith("!"):
            body = line[1:].strip()
            lower = body.lower()
            if lower.startswith("name:"):
                display_name = body[len("name:") :].strip() or display_name
            elif lower.startswith("description:"):
                description = body[len("description:") :].strip()
            # other ! lines: treat as comment
            continue

        parsed = _parse_rule_line(line)
        if isinstance(parsed, tuple) and parsed[0] == "default_discard":
            default_discard = True
            continue
        assert isinstance(parsed, Rule)
        rules.append(parsed)
        if MAX_RULES > 0 and len(rules) > MAX_RULES:
            raise GoggleParseError(f"too many rules (> {MAX_RULES})")

    return Goggle(
        name=display_name,
        description=description,
        rules=rules,
        default_discard=default_discard,
    )


_HOSTED_URL_RE = re.compile(r"^https?://[^\s]+$")


def _read_hosted_sibling(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as fp:
            text = fp.read()
    except OSError:
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if not _HOSTED_URL_RE.match(line):
            log.warning("weblibre_goggles: invalid hosted URL in %s: %r", path, line)
            return None
        return line
    return None


def _load_directory(path: str) -> dict[str, Goggle]:
    registry: dict[str, Goggle] = {}
    if not path or not os.path.isdir(path):
        return registry

    try:
        entries = sorted(os.listdir(path))
    except OSError as exc:
        log.warning("weblibre_goggles: cannot list %s: %s", path, exc)
        return registry

    for fname in entries:
        if not _FILENAME_RE.match(fname):
            continue
        if len(registry) >= MAX_GOGGLES:
            log.warning(
                "weblibre_goggles: max %d goggles reached, skipping %s",
                MAX_GOGGLES,
                fname,
            )
            break
        key = fname[: -len(".goggle")]
        full = os.path.join(path, fname)
        try:
            with open(full, "r", encoding="utf-8") as fp:
                text = fp.read()
            goggle = parse_goggle(key, text)
        except (OSError, GoggleParseError) as exc:
            log.warning("weblibre_goggles: skipping %s: %s", fname, exc)
            continue
        hosted = _read_hosted_sibling(full + ".hosted")
        if hosted:
            goggle.hosted_url = hosted
            log.info("weblibre_goggles: %s has hosted URL %s", key, hosted)
        registry[key] = goggle

    return registry


# Module-level pointer to the active plugin instance, set during init().
# Lets the braveapi engine resolve goggle names → payloads (hosted URL or
# inline text) without importing the plugin singleton via Flask state.
_PLUGIN_INSTANCE: "SXNGPlugin | None" = None


def get_goggle(name: str) -> "Goggle | None":
    """Look up a loaded goggle by registry key. Used by braveapi engine."""
    if _PLUGIN_INSTANCE is None:
        return None
    return _PLUGIN_INSTANCE.registry.get(name)


class SXNGPlugin(Plugin):
    """Apply a per-query Brave-Goggles-compatible ranking profile."""

    id = "weblibre_goggles"

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name=gettext("Goggles ranking"),
            description=gettext(
                "Filter, boost or downrank results using a server-curated "
                "Brave-Goggles-compatible profile selected per query."
            ),
            preference_section="general",
        )
        self.registry: dict[str, Goggle] = {}
        self.score_softness: float = DEFAULT_SCORE_SOFTNESS

    def init(self, app: "flask.Flask") -> bool:
        cfg = settings.get("goggles") or {}
        directory = cfg.get("dir") if isinstance(cfg, dict) else None
        if isinstance(cfg, dict):
            try:
                softness = float(cfg.get("score_softness", DEFAULT_SCORE_SOFTNESS))
            except (TypeError, ValueError):
                softness = DEFAULT_SCORE_SOFTNESS
            # Softness <= 0 would make exp() blow up or invert; reject and
            # fall back to the default rather than silently misbehaving.
            self.score_softness = softness if softness > 0 else DEFAULT_SCORE_SOFTNESS
        if not directory:
            log.debug("weblibre_goggles: no goggles.dir configured; disabling")
            return False
        self.registry = _load_directory(directory)
        if not self.registry:
            log.debug(
                "weblibre_goggles: no goggles loaded from %s; disabling", directory
            )
            return False
        log.info(
            "weblibre_goggles: loaded %d goggle(s) from %s: %s",
            len(self.registry),
            directory,
            ", ".join(sorted(self.registry.keys())),
        )
        global _PLUGIN_INSTANCE  # pylint: disable=global-statement
        _PLUGIN_INSTANCE = self
        return True

    def pre_search(
        self, request: "SXNG_Request", search: "SearchWithPlugins"
    ) -> bool:
        # Default: no goggles attached.
        setattr(search, "weblibre_goggles", [])
        try:
            raw = request.form.get("goggle") if request else None
        except Exception:  # pylint: disable=broad-except
            raw = None
        if not raw:
            return True

        names = [n.strip() for n in raw.split(",") if n.strip()]
        if len(names) > MAX_GOGGLES_PER_REQUEST:
            log.debug(
                "weblibre_goggles: truncating request from %d to %d goggles",
                len(names),
                MAX_GOGGLES_PER_REQUEST,
            )
            names = names[:MAX_GOGGLES_PER_REQUEST]

        resolved: list[Goggle] = []
        resolved_names: list[str] = []
        seen: set[str] = set()
        for name in names:
            if not _NAME_RE.match(name):
                log.debug("weblibre_goggles: rejecting invalid goggle name %r", name)
                continue
            if name in seen:
                continue
            goggle = self.registry.get(name)
            if goggle is None:
                log.debug("weblibre_goggles: unknown goggle %r", name)
                continue
            resolved.append(goggle)
            resolved_names.append(name)
            seen.add(name)

        if resolved:
            log.debug(
                "weblibre_goggles: resolved goggles for query: %s",
                [g.name for g in resolved],
            )
            # Goggles with a `<name>.goggle.hosted` sibling are handed off
            # to Brave via engine_data; the remainder are too large to ship
            # (no public URL available) and must still be applied locally
            # against braveapi results.
            hosted_names = [
                name
                for name, g in zip(resolved_names, resolved)
                if g.hosted_url
            ]
            if hosted_names:
                try:
                    search.search_query.engine_data.setdefault("braveapi", {})[
                        "weblibre_goggles"
                    ] = ",".join(hosted_names)
                except (AttributeError, TypeError) as exc:
                    log.debug(
                        "weblibre_goggles: could not write engine_data: %s", exc
                    )

        setattr(search, "weblibre_goggles", resolved)
        return True

    def on_result(
        self,
        request: "SXNG_Request",
        search: "SearchWithPlugins",
        result: "Result",
    ) -> bool:
        goggles: list[Goggle] = getattr(search, "weblibre_goggles", []) or []
        if not goggles:
            return True
        # For braveapi results, drop goggles already handed off to Brave
        # (those with a `.hosted` URL) — re-running them locally would
        # double-rank. Goggles too large to host stay in the list and are
        # applied locally as before.
        if getattr(result, "engine", "") == "braveapi":
            goggles = [g for g in goggles if not g.hosted_url]
            if not goggles:
                return True
        if not result.parsed_url:
            return True

        url = result.url or ""
        host = (result.parsed_url.netloc or "").lower()
        # Strip the optional :port so dict lookups against `$site=` rules
        # (which are bare hostnames) hit consistently.
        if ":" in host:
            host = host.split(":", 1)[0]
        host_parents = []
        if host:
            parts = host.split(".")
            for i in range(len(parts)):
                host_parents.append(".".join(parts[i:]))

        # Marginalia-shaped scoring: accumulate ±strength contributions
        # across every matching rule in every active goggle into a single
        # adjustment, then convert to a multiplier via
        # ``exp(adjustment / softness)``. Discard still wins outright (a
        # single matching discard rule drops the result, and a goggle's
        # ``default_discard`` mode drops anything not explicitly matched
        # by a non-discard rule on that goggle).
        bonus_sum = 0.0
        any_default_discard = False
        any_rule_matched = False

        def _apply_rule(rule: Rule) -> bool:
            """Apply a matching rule to the accumulators. Returns True
            when the result must be dropped (an explicit discard hit)."""
            nonlocal bonus_sum, any_rule_matched
            any_rule_matched = True
            if rule.action == "discard":
                return True
            if rule.action == "boost":
                bonus_sum += rule.strength
            elif rule.action == "downrank":
                bonus_sum -= rule.strength
            return False

        for goggle in goggles:
            if goggle.default_discard:
                any_default_discard = True

            # Fast path: hostname-keyed rules (no regex pattern).
            for candidate in host_parents:
                rules = goggle.host_index.get(candidate)
                if not rules:
                    continue
                for rule in rules:
                    if _apply_rule(rule):
                        log.debug("weblibre_goggles: discard %s", url)
                        return False

            # Slow path: rules with patterns.
            for rule in goggle.pattern_rules:
                if not rule.matches(url, host):
                    continue
                if _apply_rule(rule):
                    log.debug("weblibre_goggles: discard %s", url)
                    return False

        # Preserve original default-discard semantics: if any active goggle
        # has a bare ``$discard`` (default = discard) and no rule matched
        # this result anywhere across the active goggles, drop it.
        if any_default_discard and not any_rule_matched:
            log.debug("weblibre_goggles: default-discard %s", url)
            return False

        if bonus_sum != 0.0 and isinstance(result, (MainResult, LegacyResult)):
            multiplier = math.exp(bonus_sum / self.score_softness)
            multiplier = max(MIN_SCORE_MULTIPLIER, min(multiplier, MAX_SCORE_MULTIPLIER))
            current = getattr(result, "score_multiplier", 1.0) or 1.0
            new_value = current * multiplier
            if isinstance(result, LegacyResult):
                result["score_multiplier"] = new_value
            else:
                result.score_multiplier = new_value
            log.debug(
                "weblibre_goggles: %s adjustment=%+g multiplier=%.3f for %s",
                "boost" if bonus_sum > 0 else "downrank",
                bonus_sum,
                multiplier,
                url,
            )

        return True
