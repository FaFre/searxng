# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring, missing-class-docstring

import typing as t

import warnings
from collections import defaultdict
from threading import RLock

from searx import logger as log
import searx.engines
from searx.metrics import histogram_observe, counter_add
from searx.result_types import Result, LegacyResult, MainResult
from searx.result_types.answer import AnswerSet, BaseAnswer


def calculate_score(
    result: MainResult | LegacyResult,
    priority: MainResult.PriorityType,
    weight_overrides: dict[str, float] | None = None,
) -> float:
    # Per-engine weight calculation: if an engine is present in
    # weight_overrides, its override replaces the YAML-configured weight.
    # Engines not listed in weight_overrides fall back to their global
    # weight.  A weight override of 0 soft-disables an engine: that
    # engine's contribution is skipped entirely.  If ALL engines that
    # found a result are soft-disabled, the result score is 0 (sorted to
    # the bottom).  When a result is found by multiple engines, the
    # weights of non-disabled contributing engines are multiplied
    # together — soft-disabling one engine does not penalize the others.
    weight = 1.0
    contributing = 0

    for result_engine in result['engines']:
        if weight_overrides and result_engine in weight_overrides:
            override = weight_overrides[result_engine]
            if override == 0:
                continue
            weight *= override
            contributing += 1
        elif hasattr(searx.engines.engines.get(result_engine), 'weight'):
            weight *= float(searx.engines.engines[result_engine].weight)
            contributing += 1
        else:
            weight *= 1.0
            contributing += 1

    if contributing == 0:
        return 0

    weight *= len(result['positions'])
    score = 0

    for position in result['positions']:
        if priority == 'low':
            continue
        if priority == 'high':
            score += weight
        else:
            score += weight / position

    # Per-result score multiplier (e.g. from the weblibre_goggles plugin's
    # fine-grained boost/downrank). Default 1.0 is a no-op for any result
    # that doesn't set the field. Clamped to a sane range so a pathological
    # goggle can't produce ±inf and corrupt sort order.
    multiplier = 1.0
    if isinstance(result, dict):
        multiplier = result.get('score_multiplier', 1.0)
    else:
        multiplier = getattr(result, 'score_multiplier', 1.0)
    if multiplier != 1.0:
        score *= max(1e-3, min(float(multiplier), 1e3))

    return score


class Timing(t.NamedTuple):
    engine: str
    total: float
    load: float


class UnresponsiveEngine(t.NamedTuple):
    engine: str
    error_type: str
    suspended: bool


class ResultContainer:
    """In the result container, the results are collected, sorted and duplicates
    will be merged."""

    # pylint: disable=too-many-statements

    main_results_map: dict[int, MainResult | LegacyResult]
    infoboxes: list[LegacyResult]
    suggestions: set[str]
    answers: AnswerSet
    corrections: set[str]

    def __init__(self, weight_overrides: dict[str, float] | None = None):
        self.main_results_map = {}
        self.infoboxes = []
        self.suggestions = set()
        self.answers = AnswerSet()
        self.corrections = set()

        self._number_of_results: list[int] = []
        self.engine_data: dict[str, dict[str, str]] = defaultdict(dict)
        self._closed: bool = False
        self.paging: bool = False
        self.unresponsive_engines: set[UnresponsiveEngine] = set()
        self.timings: list[Timing] = []
        self.redirect_url: str | None = None
        self.on_result: t.Callable[[Result | LegacyResult], bool] = lambda _: True
        self._lock: RLock = RLock()
        self._main_results_sorted: list[MainResult | LegacyResult] | None = None
        self.weight_overrides: dict[str, float] = weight_overrides or {}

    def extend(
        self, engine_name: str | None, results: list[Result | LegacyResult]
    ):  # pylint: disable=too-many-branches
        if self._closed:
            log.debug("container is closed, ignoring results: %s", results)
            return
        main_count = 0

        for result in list(results):

            if isinstance(result, Result):
                result.engine = result.engine or engine_name
                result.normalize_result_fields()
                if not self.on_result(result):
                    continue

                if isinstance(result, BaseAnswer):
                    self.answers.add(result)
                elif isinstance(result, MainResult):
                    main_count += 1
                    self._merge_main_result(result, main_count)
                else:
                    # more types need to be implemented in the future ..
                    raise NotImplementedError(f"no handler implemented to process the result of type {result}")

            else:
                result["engine"] = result.get("engine") or engine_name or ""
                result = LegacyResult(result)  # for backward compatibility, will be romeved one day
                result.normalize_result_fields()

                if "suggestion" in result:
                    if self.on_result(result):
                        self.suggestions.add(result["suggestion"])
                    continue

                if "answer" in result:
                    if self.on_result(result):
                        warnings.warn(
                            f"answer results from engine {result.engine}"
                            " are without typification / migrate to Answer class.",
                            DeprecationWarning,
                        )
                        self.answers.add(result)  # type: ignore
                    continue

                if "correction" in result:
                    if self.on_result(result):
                        self.corrections.add(result["correction"])
                    continue

                if "infobox" in result:
                    if self.on_result(result):
                        self._merge_infobox(result)
                    continue

                if "number_of_results" in result:
                    if self.on_result(result):
                        self._number_of_results.append(result["number_of_results"])
                    continue

                if "engine_data" in result:
                    if self.on_result(result):
                        if result.engine:
                            self.engine_data[result.engine][result["key"]] = result["engine_data"]
                    continue

                if self.on_result(result):
                    main_count += 1
                    self._merge_main_result(result, main_count)
                    continue

        if engine_name in searx.engines.engines:
            eng = searx.engines.engines[engine_name]
            histogram_observe(main_count, "engine", eng.name, "result", "count")
            if not self.paging and eng.paging:
                self.paging = True

    def _merge_infobox(self, new_infobox: LegacyResult):
        add_infobox = True

        new_id = getattr(new_infobox, "id", None)
        if new_id is not None:
            with self._lock:
                for existing_infobox in self.infoboxes:
                    if new_id == getattr(existing_infobox, "id", None):
                        merge_two_infoboxes(existing_infobox, new_infobox, self.weight_overrides)
                        add_infobox = False
        if add_infobox:
            self.infoboxes.append(new_infobox)

    def _merge_main_result(self, result: MainResult | LegacyResult, position: int):
        result_hash = hash(result)

        with self._lock:

            merged = self.main_results_map.get(result_hash)
            if not merged:
                # if there is no duplicate in the merged results, append result
                result.positions = [position]
                self.main_results_map[result_hash] = result
                return

            merge_two_main_results(merged, result)
            # add the new position
            merged.positions.append(position)

    def close(self):
        self._closed = True

        for result in self.main_results_map.values():
            result.score = calculate_score(result, result.priority, self.weight_overrides)
            for eng_name in result.engines:
                counter_add(result.score, 'engine', eng_name, 'score')

    def get_ordered_results(self) -> list[MainResult | LegacyResult]:
        """Returns a sorted list of results to be displayed in the main result
        area (:ref:`result types`)."""

        if not self._closed:
            self.close()

        if self._main_results_sorted:
            return self._main_results_sorted

        # first pass, sort results by "score" (descanding)
        results = sorted(self.main_results_map.values(), key=lambda x: x.score, reverse=True)

        # pass 2 : group results by category and template
        gresults: list[MainResult | LegacyResult] = []
        categoryPositions: dict[str, t.Any] = {}
        max_count = 8
        max_distance = 20

        for res in results:
            # do we need to handle more than one category per engine?
            engine = searx.engines.engines.get(res.engine or "")
            if engine:
                res.category = engine.categories[0] if len(engine.categories) > 0 else ""

            # do we need to handle more than one category per engine?
            category = f"{res.category}:{res.template}:{'img_src' if (res.thumbnail or res.img_src) else ''}"
            grp = categoryPositions.get(category)

            # group with previous results using the same category, if the group
            # can accept more result and is not too far from the current
            # position

            if (grp is not None) and (grp["count"] > 0) and (len(gresults) - grp["index"] < max_distance):
                # group with the previous results using the same category with
                # this one
                index = grp["index"]
                gresults.insert(index, res)

                # update every index after the current one (including the
                # current one)
                for item in categoryPositions.values():
                    v = item["index"]
                    if v >= index:
                        item["index"] = v + 1

                # update this category
                grp["count"] -= 1

            else:
                gresults.append(res)
                # update categoryIndex
                categoryPositions[category] = {"index": len(gresults), "count": max_count}
                continue

        self._main_results_sorted = gresults
        return self._main_results_sorted

    @property
    def number_of_results(self) -> int:
        """Returns the average of results number, returns zero if the average
        result number is smaller than the actual result count."""

        if not self._closed:
            log.error("call to ResultContainer.number_of_results before ResultContainer.close")
            return 0

        with self._lock:
            resultnum_sum = sum(self._number_of_results)
            if not resultnum_sum or not self._number_of_results:
                return 0

            average = int(resultnum_sum / len(self._number_of_results))
            if average < len(self.get_ordered_results()):
                average = 0
            return average

    def add_unresponsive_engine(self, engine_name: str, error_type: str, suspended: bool = False):
        with self._lock:
            if self._closed:
                log.error("call to ResultContainer.add_unresponsive_engine after ResultContainer.close")
                return
            if searx.engines.engines[engine_name].display_error_messages:
                self.unresponsive_engines.add(UnresponsiveEngine(engine_name, error_type, suspended))

    def add_timing(self, engine_name: str, engine_time: float, page_load_time: float):
        with self._lock:
            if self._closed:
                log.error("call to ResultContainer.add_timing after ResultContainer.close")
                return
            self.timings.append(Timing(engine_name, total=engine_time, load=page_load_time))

    def get_timings(self) -> list[Timing]:
        with self._lock:
            if not self._closed:
                log.error("call to ResultContainer.get_timings before ResultContainer.close")
                return []
            return self.timings


def merge_two_infoboxes(
    origin: LegacyResult, other: LegacyResult, weight_overrides: dict[str, float] | None = None
):
    """Merges the values from ``other`` into ``origin``."""
    # pylint: disable=too-many-branches
    # A weight override of 0 soft-disables an engine for *scoring*, but
    # for infobox merging we still need to pick which infobox wins.
    # Treating 0 as 0 here would make ties "origin wins" arbitrarily and
    # leak the soft-disable into a separate decision.  Fall back to the
    # YAML weight so the admin's configured ranking still drives merges.
    def _merge_weight(engine_name: str) -> float:
        if weight_overrides and engine_name in weight_overrides:
            override = weight_overrides[engine_name]
            if override != 0:
                return override
        return float(getattr(searx.engines.engines[engine_name], "weight", 1))

    weight1 = _merge_weight(origin.engine)
    weight2 = _merge_weight(other.engine)

    if weight2 > weight1:
        origin.engine = other.engine

    origin.engines |= other.engines

    if other.urls:
        url_items = origin.get("urls", [])

        for url2 in other.urls:
            unique_url = True
            entity_url2 = url2.get("entity")

            for url1 in origin.get("urls", []):
                if (entity_url2 is not None and entity_url2 == url1.get("entity")) or (
                    url1.get("url") == url2.get("url")
                ):
                    unique_url = False
                    break
            if unique_url:
                url_items.append(url2)

        origin.urls = url_items

    if other.img_src:
        if not origin.img_src:
            origin.img_src = other.img_src
        elif weight2 > weight1:
            origin.img_src = other.img_src

    if other.attributes:
        if not origin.attributes:
            origin.attributes = other.attributes
        else:
            attr_names_1: set[str] = set()
            for attr in origin.attributes:
                label = attr.get("label")
                if label:
                    attr_names_1.add(label)

                entity = attr.get("entity")
                if entity:
                    attr_names_1.add(entity)

            for attr in other.attributes:
                if attr.get("label") not in attr_names_1 and attr.get('entity') not in attr_names_1:
                    origin.attributes.append(attr)

    if other.content:
        if not origin.content:
            origin.content = other.content
        elif len(other.content) > len(origin.content):
            origin.content = other.content


def merge_two_main_results(origin: MainResult | LegacyResult, other: MainResult | LegacyResult):
    """Merges the values from ``other`` into ``origin``."""

    if len(other.content or "") > len(origin.content or ""):
        # use content with more text
        origin.content = other.content

    # use title with more text
    if len(other.title or "") > len(origin.title or ""):
        origin.title = other.title

    # merge all result's parameters not found in origin
    if isinstance(other, MainResult) and isinstance(origin, MainResult):
        origin.defaults_from(other)
    elif isinstance(other, LegacyResult) and isinstance(origin, LegacyResult):
        origin.defaults_from(other)

    # accumulate metadata from both results
    if isinstance(origin, MainResult) and isinstance(other, MainResult):
        if other.metadata and other.metadata not in (origin.metadata or ""):
            if origin.metadata:
                origin.metadata = origin.metadata + " / " + other.metadata
            else:
                origin.metadata = other.metadata
        if other.metadata_items:
            existing_keys = {(item["key"], item["value"]) for item in origin.metadata_items}
            for item in other.metadata_items:
                if (item["key"], item["value"]) not in existing_keys:
                    origin.metadata_items.append(item)
    elif isinstance(origin, LegacyResult) and isinstance(other, LegacyResult):
        other_metadata = other.get("metadata", "")
        if other_metadata and other_metadata not in (origin.get("metadata", "") or ""):
            origin_metadata = origin.get("metadata", "")
            if origin_metadata:
                origin["metadata"] = origin_metadata + " / " + other_metadata
            else:
                origin["metadata"] = other_metadata

    # add engine to list of result-engines
    origin.engines.add(other.engine or "")

    # use https, ftps, .. if possible
    if origin.parsed_url and not origin.parsed_url.scheme.endswith("s"):
        if other.parsed_url and other.parsed_url.scheme.endswith("s"):
            origin.parsed_url = origin.parsed_url._replace(scheme=other.parsed_url.scheme)
            origin.url = origin.parsed_url.geturl()
