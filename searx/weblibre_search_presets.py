# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared WebLibre search presets for the local SearXNG UI.

The WebLibre backend and the SearXNG GUI both consume the same JSON file so
operators can tune one set of engine/weight/goggle presets and compare them
directly in the browser.

The file is named ``search_presets.json`` and is read once at startup from the
SearXNG configuration folder: the folder of ``SEARXNG_SETTINGS_PATH``, by
default ``/etc/searxng``.  If the file does not exist, presets are disabled and
the preset selector is hidden.  A file that exists but is invalid aborts the
startup.

.. code:: json

   {
     "presets": {
       "balanced": {
         "label": "Balanced",
         "description": "Brave and Mojeek, Brave weighted up",
         "engines": ["braveapi", "mojeekapi"],
         "weightOverrides": {"braveapi": 1.5},
         "goggles": ["tech"]
       }
     }
   }

A request selects a preset with the ``preset`` parameter.
:py:func:`apply_preset` expands it into the regular ``engines``,
``weight_overrides`` and ``goggle`` parameters *before* the query is parsed, so
a preset goes through the same validation as a hand-written request (engine
tokens, unknown engines, weight limits).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import typing as t

from searx import logger
from searx import settings_loader
from searx.engines import engines
from searx.webadapter import parse_category_form


log = logger.getChild('weblibre_search_presets')

PRESET_FILE_NAME = 'search_presets.json'
_PRESET_ID_RE = re.compile(r'^[A-Za-z0-9_\-]{1,64}$')


@dataclass(frozen=True)
class SearchPreset:
    id: str
    label: str
    description: str
    engines: tuple[str, ...]
    weight_overrides: dict[str, float]
    goggles: tuple[str, ...]

    @property
    def engines_param(self) -> str:
        return ','.join(self.engines)

    @property
    def weight_overrides_param(self) -> str:
        return ','.join(f'{engine}:{weight}' for engine, weight in self.weight_overrides.items())

    @property
    def goggle_param(self) -> str:
        return ','.join(self.goggles)


_PRESETS_BY_ID: dict[str, SearchPreset] = {}
_PRESET_LIST: tuple[SearchPreset, ...] = ()


def get_default_path() -> Path | None:
    """Location of :py:obj:`PRESET_FILE_NAME` in the SearXNG configuration
    folder, or ``None`` if there is no such folder."""
    folder = settings_loader.get_user_cfg_folder()
    return folder / PRESET_FILE_NAME if folder else None


def initialize(path: str | os.PathLike[str] | None = None) -> None:
    """Load the presets.

    Without ``path``, the file is looked up with :py:func:`get_default_path`
    and a missing file disables presets.  An explicit ``path`` must exist.
    """
    global _PRESETS_BY_ID, _PRESET_LIST  # pylint: disable=global-statement
    _PRESET_LIST = ()
    _PRESETS_BY_ID = {}

    if path is None:
        path = get_default_path()
        if path is None or not path.is_file():
            log.info('weblibre_search_presets: no %s found, search presets are disabled', PRESET_FILE_NAME)
            return

    with open(path, 'r', encoding='utf-8') as fp:
        decoded = json.load(fp)

    if not isinstance(decoded, dict):
        raise ValueError(f'WebLibre search preset config must be an object: {path}')

    presets_json = decoded.get('presets')
    if not isinstance(presets_json, dict):
        raise ValueError(f'WebLibre search preset config is missing presets: {path}')

    preset_list: list[SearchPreset] = []
    for preset_id, preset_json in presets_json.items():
        if not isinstance(preset_id, str) or not _PRESET_ID_RE.match(preset_id):
            raise ValueError(f'invalid preset id {preset_id!r}')
        if not isinstance(preset_json, dict):
            raise ValueError(f'presets.{preset_id} must be an object')
        preset_list.append(_parse_preset(preset_id, preset_json))

    _PRESET_LIST = tuple(preset_list)
    _PRESETS_BY_ID = {preset.id: preset for preset in preset_list}
    log.info(
        'weblibre_search_presets: loaded %d preset(s) from %s: %s',
        len(_PRESET_LIST),
        path,
        ', '.join(preset.id for preset in _PRESET_LIST),
    )


def get_presets() -> tuple[SearchPreset, ...]:
    return _PRESET_LIST


def get_preset(preset_id: str | None) -> SearchPreset | None:
    if not preset_id:
        return None
    return _PRESETS_BY_ID.get(preset_id.strip())


def get_effective_form_values(form: dict[str, str]) -> tuple[str, str, str]:
    current_engines = (form.get('engines') or '').strip()
    current_weight_overrides = (form.get('weight_overrides') or '').strip()
    current_goggle = (form.get('goggle') or '').strip()

    if current_engines or current_weight_overrides or current_goggle:
        return current_engines, current_weight_overrides, current_goggle

    preset = get_preset(form.get('preset'))
    if preset is None:
        return '', '', ''

    return preset.engines_param, preset.weight_overrides_param, preset.goggle_param


def get_extra_categories(form: dict[str, str]) -> list[str]:
    """Categories requested together with an explicit ``engines`` parameter.

    The query parser selects the listed engines *plus* every engine of these
    categories (see ``webadapter.parse_generic``), so forms and URLs that
    repeat the search must carry them next to the engine list.  Presets never
    have extra categories: :py:func:`apply_preset` removes them.
    """
    if not (form.get('engines') or '').strip():
        return []
    extra_categories: list[str] = []
    for name, value in form.items():
        parse_category_form(extra_categories, name, value)
    return list(dict.fromkeys(extra_categories))


def match_preset(form: dict[str, str]) -> SearchPreset | None:
    current_engines, current_weight_overrides, current_goggle = get_effective_form_values(form)

    if not current_engines and not current_weight_overrides and not current_goggle:
        return None
    if get_extra_categories(form):
        # engines plus categories select more than the preset's engine list
        return None

    parsed_engines = _parse_csv(current_engines)
    parsed_goggles = _parse_csv(current_goggle)
    parsed_weights = _parse_weight_overrides(current_weight_overrides)

    for preset in _PRESET_LIST:
        if (
            parsed_engines == list(preset.engines)
            and parsed_goggles == list(preset.goggles)
            and parsed_weights == preset.weight_overrides
        ):
            return preset

    return None


def apply_preset(form: dict[str, str]) -> SearchPreset | None:
    """Expand the ``preset`` form parameter into the ``engines``,
    ``weight_overrides`` and ``goggle`` form parameters.

    Must run *before* :py:func:`searx.webadapter.get_search_query_from_webapp`
    so the preset goes through the regular validation: engines without a valid
    token are dropped, unknown engines are ignored and weights are clamped.
    Explicit ``engines``, ``weight_overrides`` or ``goggle`` parameters take
    precedence and leave the form untouched.
    """
    if form.get('engines') or form.get('weight_overrides') or form.get('goggle'):
        return None

    preset = get_preset(form.get('preset'))
    if preset is None:
        return None

    form['engines'] = preset.engines_param
    if preset.weight_overrides:
        form['weight_overrides'] = preset.weight_overrides_param
    else:
        form.pop('weight_overrides', None)
    if preset.goggles:
        form['goggle'] = preset.goggle_param
    else:
        form.pop('goggle', None)

    # An explicit engine list combined with categories selects the listed
    # engines *plus* every engine of those categories (see
    # ``webadapter.parse_generic``), which would widen the preset.
    for key in [key for key in form if key == 'categories' or key.startswith('category_')]:
        del form[key]

    return preset


def _parse_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(',') if item.strip()]


def _parse_weight_overrides(raw: str) -> dict[str, float]:
    parsed: dict[str, float] = {}
    for pair in _parse_csv(raw):
        if ':' not in pair:
            continue
        engine_name, _, weight_str = pair.partition(':')
        engine_name = engine_name.strip()
        if not engine_name:
            continue
        try:
            parsed[engine_name] = float(weight_str)
        except ValueError:
            continue
    return parsed


def _parse_preset(preset_id: str, preset_json: dict[str, t.Any]) -> SearchPreset:
    base_path = f'presets.{preset_id}'
    engines_list = _require_string_list(preset_json, 'engines', parent=base_path)
    if not engines_list:
        raise ValueError(f'{base_path}.engines must not be empty')

    missing_engines = [engine_name for engine_name in engines_list if engine_name not in engines]
    if missing_engines:
        raise ValueError(f'{base_path} references unknown engines: {missing_engines!r}')

    weights_json = preset_json.get('weightOverrides')
    if not isinstance(weights_json, dict):
        raise ValueError(f'{base_path}.weightOverrides must be an object')

    weight_overrides: dict[str, float] = {}
    for engine_name, value in weights_json.items():
        if engine_name not in engines_list:
            raise ValueError(
                f'{base_path}.weightOverrides.{engine_name} must target one of the preset engines'
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f'{base_path}.weightOverrides.{engine_name} must be numeric')
        if not math.isfinite(value) or value < 0:
            raise ValueError(f'{base_path}.weightOverrides.{engine_name} must be a finite number >= 0')
        weight_overrides[engine_name] = float(value)

    return SearchPreset(
        id=preset_id,
        label=_require_string(preset_json.get('label'), f'{base_path}.label'),
        description=_require_string(preset_json.get('description'), f'{base_path}.description'),
        engines=tuple(engines_list),
        weight_overrides=weight_overrides,
        goggles=tuple(_require_string_list(preset_json, 'goggles', parent=base_path)),
    )


def _require_string(value: t.Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f'{path} must be a non-empty string')
    return value


def _require_string_list(
    data: dict[str, t.Any],
    key: str,
    *,
    parent: str,
) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ValueError(f'{parent}.{key} must be a list')

    items: list[str] = []
    for idx, item in enumerate(value):
        items.append(_require_string(item, f'{parent}.{key}[{idx}]'))
    return items
