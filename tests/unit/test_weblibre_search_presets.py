# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring,invalid-name

import json
import tempfile
from pathlib import Path

import searx.plugins
from searx import weblibre_search_presets
from searx.engines import engines
from searx.preferences import Preferences
from searx.webadapter import get_search_query_from_webapp

from tests import SearxTestCase

PUBLIC_ENGINE = "dummy engine"
PRIVATE_ENGINE = "dummy private engine"  # tokens: ["my-token"] in test_settings.yml


def _preset(engine_names, weights=None, goggles=None):
    return {
        "label": "Label",
        "description": "Description",
        "engines": engine_names,
        "weightOverrides": weights or {},
        "goggles": goggles or [],
    }


class TestSearchPresets(SearxTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self.tmp.cleanup)
        # leave the module with presets disabled, as at test startup
        self.addCleanup(weblibre_search_presets.initialize)

    def _load(self, presets):
        path = Path(self.tmp.name) / "search_presets.json"
        path.write_text(json.dumps({"presets": presets}), encoding="utf-8")
        weblibre_search_presets.initialize(path)

    def _search_query(self, form):
        preferences = Preferences(['simple'], ['general'], engines, searx.plugins.STORAGE)
        weblibre_search_presets.apply_preset(form)
        return get_search_query_from_webapp(preferences, form)[0]

    def test_missing_default_file_disables_presets(self):
        self._load({"p": _preset([PUBLIC_ENGINE])})
        self.assertEqual(len(weblibre_search_presets.get_presets()), 1)

        # tests/unit/settings/ has no search_presets.json
        weblibre_search_presets.initialize()
        self.assertEqual(weblibre_search_presets.get_presets(), ())
        self.assertIsNone(weblibre_search_presets.get_preset("p"))

    def test_missing_explicit_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            weblibre_search_presets.initialize(Path(self.tmp.name) / "missing.json")

    def test_invalid_file_raises(self):
        with self.assertRaises(ValueError):
            self._load({"p": _preset(["no such engine"])})
        with self.assertRaises(ValueError):
            self._load({"p": _preset([PUBLIC_ENGINE], weights={PUBLIC_ENGINE: -1})})

    def test_apply_preset_expands_form_and_drops_categories(self):
        self._load({"p": _preset([PUBLIC_ENGINE], weights={PUBLIC_ENGINE: 2.5}, goggles=["tech"])})
        form = {"q": "test", "preset": "p", "category_images": "1", "categories": "general"}

        preset = weblibre_search_presets.apply_preset(form)

        self.assertIsNotNone(preset)
        self.assertEqual(
            form,
            {
                "q": "test",
                "preset": "p",
                "engines": PUBLIC_ENGINE,
                "weight_overrides": f"{PUBLIC_ENGINE}:2.5",
                "goggle": "tech",
            },
        )

    def test_explicit_parameters_win_over_preset(self):
        self._load({"p": _preset([PUBLIC_ENGINE], goggles=["tech"])})
        form = {"q": "test", "preset": "p", "goggle": "other"}

        self.assertIsNone(weblibre_search_presets.apply_preset(form))
        self.assertEqual(form, {"q": "test", "preset": "p", "goggle": "other"})

    def test_preset_engines_go_through_validation(self):
        self._load({"p": _preset([PUBLIC_ENGINE, PRIVATE_ENGINE], weights={PUBLIC_ENGINE: 5000})})

        search_query = self._search_query({"q": "test", "preset": "p"})

        # the token protected engine is dropped, the weight is clamped
        self.assertEqual([ref.name for ref in search_query.engineref_list], [PUBLIC_ENGINE])
        self.assertEqual(search_query.weight_overrides, {PUBLIC_ENGINE: 1000.0})

    def test_extra_categories_only_with_engine_list(self):
        form = {"q": "test", "engines": PUBLIC_ENGINE, "category_general": "1", "categories": "general,unknown"}
        self.assertEqual(weblibre_search_presets.get_extra_categories(form), ["general"])
        self.assertEqual(weblibre_search_presets.get_extra_categories({"q": "test", "category_general": "1"}), [])

    def test_engine_list_with_categories_is_no_preset(self):
        self._load({"p": _preset([PUBLIC_ENGINE])})
        self.assertEqual(weblibre_search_presets.match_preset({"engines": PUBLIC_ENGINE}).id, "p")
        self.assertIsNone(weblibre_search_presets.match_preset({"engines": PUBLIC_ENGINE, "category_general": "1"}))
