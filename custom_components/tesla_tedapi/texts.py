"""Texts the integration builds itself (alert descriptions, the Active alerts
summary, logbook messages), in Home Assistant's language.

Entity names, states, repairs and errors are translated by Home Assistant from
strings.json. These texts are put together here instead, so they are looked up
in the "selector" section of the translations, keyed by group and option:
selector.<group>.options.<key>. Anything missing falls back to the English
text the caller passes, so a partial translation never shows a blank."""
from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.const import EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DATA_TEXTS = f"{DOMAIN}_texts"

# Groups in strings.json -> selector.
ALERT_DESCRIPTION = "alert_description"
ALERT_SHORT = "alert_short"
LOGBOOK = "logbook"
DISPATCH_END_REASON = "dispatch_end_reason"
OFF_GRID_END_REASON = "off_grid_end_reason"


class Texts:
    """Translated texts for one language; English (the caller's default)
    for anything not found."""

    def __init__(self, strings: Mapping[str, str] | None = None, language: str = "en") -> None:
        self.strings: Mapping[str, str] = strings or {}
        self.language = language

    def get(self, group: str, key: str, default: str, **placeholders: Any) -> str:
        text = self.strings.get(f"component.{DOMAIN}.selector.{group}.options.{key.lower()}") or default
        if placeholders:
            try:
                return text.format(**placeholders)
            except (KeyError, IndexError, ValueError):
                # A translation with a wrong placeholder: use the English text.
                return default.format(**placeholders)
        return text


ENGLISH = Texts()


async def _async_read(hass: HomeAssistant) -> Texts:
    language = hass.config.language or "en"
    try:
        strings = await async_get_translations(hass, language, "selector", [DOMAIN])
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not load %s texts: %s", language, err)
        return Texts(language=language)
    return Texts(strings, language)


async def async_setup_texts(hass: HomeAssistant) -> None:
    """Load the texts for Home Assistant's language, and again when the
    language changes."""
    holder = hass.data.setdefault(DATA_TEXTS, Texts())
    loaded = await _async_read(hass)
    holder.strings, holder.language = loaded.strings, loaded.language

    async def _language_changed(event: Event) -> None:
        if (hass.config.language or "en") != holder.language:
            loaded = await _async_read(hass)
            holder.strings, holder.language = loaded.strings, loaded.language

    @callback
    def _listen(event: Event) -> None:
        hass.async_create_task(_language_changed(event))

    hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, _listen)


@callback
def texts(hass: HomeAssistant) -> Texts:
    """The shared texts (English until they're loaded). The same object is
    updated in place when the language changes."""
    return hass.data.setdefault(DATA_TEXTS, Texts())
