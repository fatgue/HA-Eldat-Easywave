"""Translation and manifest consistency.

Missing or drifted translation keys are a classic source of blank labels in the
Home Assistant UI, and they are invisible until someone opens the affected form.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parent.parent / "custom_components" / "eldat_easywave"
STRINGS = COMPONENT / "strings.json"
TRANSLATIONS = COMPONENT / "translations"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def key_paths(data, prefix: str = "") -> set[str]:
    """Every leaf path in a nested mapping."""
    paths: set[str] = set()
    if isinstance(data, dict):
        for key, value in data.items():
            paths |= key_paths(value, f"{prefix}.{key}" if prefix else key)
    else:
        paths.add(prefix)
    return paths


@pytest.fixture(scope="module")
def strings() -> dict:
    return load(STRINGS)


class TestManifest:
    def test_required_fields(self):
        manifest = load(COMPONENT / "manifest.json")
        assert manifest["domain"] == "eldat_easywave"
        assert manifest["config_flow"] is True
        # local_push: the stick reports telegrams unprompted.
        assert manifest["iot_class"] == "local_push"
        assert manifest["version"]
        for field in ("name", "documentation", "issue_tracker", "codeowners"):
            assert manifest[field]

    def test_no_third_party_requirements(self):
        """The whole point of the bridge add-on split.

        The Home Assistant container has no libusb, so the integration must not
        need anything beyond the standard library.
        """
        assert load(COMPONENT / "manifest.json")["requirements"] == []


class TestTranslations:
    def test_english_matches_strings(self, strings):
        assert key_paths(load(TRANSLATIONS / "en.json")) == key_paths(strings)

    def test_german_is_complete(self, strings):
        german = key_paths(load(TRANSLATIONS / "de.json"))
        missing = key_paths(strings) - german
        assert not missing, f"missing German translations: {sorted(missing)}"

    def test_german_has_no_extra_keys(self, strings):
        extra = key_paths(load(TRANSLATIONS / "de.json")) - key_paths(strings)
        assert not extra, f"stale German keys: {sorted(extra)}"

    def test_no_empty_strings(self):
        for path in TRANSLATIONS.glob("*.json"):
            for key, value in _leaves(load(path)):
                assert value.strip(), f"{path.name}: {key} is empty"


class TestCodeAndStringsAgree:
    """Guards against forms that render without labels."""

    def test_every_subentry_type_is_described(self, strings):
        from custom_components.eldat_easywave.config_flow import EldatConfigFlow

        declared = set(EldatConfigFlow.async_get_supported_subentry_types(None))
        described = set(strings["config_subentries"])
        assert declared == described

    def test_every_service_is_described(self, strings):
        services = _yaml_keys(COMPONENT / "services.yaml")
        assert set(services) == set(strings["services"])

    def test_config_flow_errors_are_described(self, strings):
        assert "cannot_connect" in strings["config"]["error"]
        for subentry in ("contact", "transmitter"):
            assert "invalid_address" in strings["config_subentries"][subentry]["error"]

    def test_contact_device_classes_are_described(self, strings):
        from custom_components.eldat_easywave.config_flow import (
            _CONTACT_DEVICE_CLASSES,
        )

        described = strings["selector"]["contact_device_class"]["options"]
        assert set(_CONTACT_DEVICE_CLASSES) == set(described)

    def test_event_actions_are_described(self, strings):
        from custom_components.eldat_easywave.eldat.telegrams import Action

        described = strings["entity"]["event"]["transmitter_key"]["state_attributes"][
            "event_type"
        ]["state"]
        assert {str(action) for action in Action} == set(described)


def _leaves(data, prefix: str = ""):
    if isinstance(data, dict):
        for key, value in data.items():
            yield from _leaves(value, f"{prefix}.{key}" if prefix else key)
    elif isinstance(data, str):
        yield prefix, data


def _yaml_keys(path: Path) -> dict:
    """Parse services.yaml the way Home Assistant does."""
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestPositionRange:
    """Transmit positions are 0-based -- measured, not documented.

    A stick reporting 64 positions accepts TXP,00 through TXP,3F and answers
    ERROR for TXP,40. Offering 1..count would both expose a position the stick
    refuses and hide one that works.
    """

    def test_selector_spans_zero_to_count_minus_one(self):
        from custom_components.eldat_easywave.config_flow import _position_selector

        config = _position_selector(64).config
        assert config["min"] == 0
        assert config["max"] == 63

    def test_selector_follows_a_128_position_stick(self):
        from custom_components.eldat_easywave.config_flow import _position_selector

        config = _position_selector(128).config
        assert (config["min"], config["max"]) == (0, 127)

    def test_default_position_is_valid(self):
        from custom_components.eldat_easywave.const import FIRST_POSITION

        assert FIRST_POSITION == 0

    def test_service_accepts_position_zero(self):
        """1..255 would have rejected a perfectly valid position."""
        from custom_components.eldat_easywave import _SEND_TELEGRAM_SCHEMA

        assert _SEND_TELEGRAM_SCHEMA({"position": 0, "key": "A"})["position"] == 0

    def test_service_rejects_negative_position(self):
        import voluptuous as vol

        from custom_components.eldat_easywave import _SEND_TELEGRAM_SCHEMA

        with pytest.raises(vol.Invalid):
            _SEND_TELEGRAM_SCHEMA({"position": -1, "key": "A"})


class TestServicesYaml:
    """Guards a YAML trap that hassfest caught and a hand-read would not.

    In YAML 1.1 a bare ``on`` is the boolean true, so ``on:`` as a field name
    silently becomes the key ``True`` and Home Assistant rejects the file. The
    key must stay quoted.
    """

    @pytest.fixture(scope="class")
    def services(self) -> dict:
        return _yaml_keys(COMPONENT / "services.yaml")

    def test_led_field_is_the_string_on_not_a_boolean(self, services):
        fields = services["set_led"]["fields"]
        assert "on" in fields, f"expected a string key, got {list(fields)}"
        assert True not in fields

    def test_service_field_names_match_the_schemas(self, services):
        from custom_components.eldat_easywave import (
            _SEND_TELEGRAM_SCHEMA,
            _SET_LED_SCHEMA,
        )

        for name, schema in (
            ("send_telegram", _SEND_TELEGRAM_SCHEMA),
            ("set_led", _SET_LED_SCHEMA),
        ):
            declared = {str(key) for key in schema.schema}
            documented = set(services[name]["fields"])
            assert declared == documented, f"{name}: {declared} != {documented}"

    def test_position_selectors_start_at_zero(self, services):
        """Positions are 0-based; the UI must not offer 1 as the minimum."""
        selector = services["send_telegram"]["fields"]["position"]["selector"]
        assert selector["number"]["min"] == 0
