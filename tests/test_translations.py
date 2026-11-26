"""Every translation has exactly the keys of strings.json (2.9.0), and
translations/en.json is strings.json."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import pytest

from custom_components.tesla_tedapi.const import DOMAIN

FOLDER = Path(__file__).parents[1] / "custom_components" / DOMAIN


def _paths(value: dict[str, Any], prefix: str = "") -> set[str]:
    out: set[str] = set()
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        out |= _paths(item, path) if isinstance(item, dict) else {path}
    return out


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_english_is_strings_json() -> None:
    assert _load(FOLDER / "translations" / "en.json") == _load(FOLDER / "strings.json")


@pytest.mark.parametrize("path", sorted((FOLDER / "translations").glob("*.json")), ids=lambda p: p.name)
def test_same_keys_as_strings_json(path: Path) -> None:
    expected = _paths(_load(FOLDER / "strings.json"))
    found = _paths(_load(path))
    assert not expected - found, f"missing in {path.name}: {sorted(expected - found)[:10]}"
    assert not found - expected, f"not in strings.json: {sorted(found - expected)[:10]}"


# hassfest refuses strings that look like HTML (2.9.0: "<base>" in a field's
# help failed the Validate workflow).
HTML = re.compile(r"<[a-zA-Z/][^<>]*>")


def _strings(value: Any, path: str = "") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        return [item for key, child in value.items() for item in _strings(child, f"{path}.{key}" if path else key)]
    return [(path, value)] if isinstance(value, str) else []


@pytest.mark.parametrize("path", [FOLDER / "strings.json", *sorted((FOLDER / "translations").glob("*.json"))],
                         ids=lambda p: p.name)
def test_no_html(path: Path) -> None:
    found = [(where, text[:60]) for where, text in _strings(_load(path)) if HTML.search(text)]
    assert not found, found
