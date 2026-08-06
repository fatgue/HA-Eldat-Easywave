"""Integration tests: the component loaded into a real Home Assistant.

Exercises the wiring that unit tests cannot reach -- config flow, subentry to
entity mapping, optimistic state and telegram-driven state.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.const import STATE_CLOSED, STATE_OFF, STATE_ON, STATE_OPEN, Platform
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eldat_easywave.const import (
    CONF_ADDRESS,
    CONF_CONNECTION,
    CONF_HOST,
    CONF_KEY,
    CONF_KEY_STATE_OFF,
    CONF_KEY_STATE_ON,
    CONF_PORT,
    CONF_POSITION,
    CONNECTION_TCP,
    DOMAIN,
    SERVICE_SEND_TELEGRAM,
    SUBENTRY_BUTTON,
    SUBENTRY_CONTACT,
    SUBENTRY_COVER,
    SUBENTRY_SWITCH,
    SUBENTRY_TRANSMITTER,
)
from custom_components.eldat_easywave.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.eldat_easywave.eldat.parser import Identification, Info
from custom_components.eldat_easywave.eldat.telegrams import Action, TelegramEvent

WINDOW_ADDRESS = "1a2b3c4d"


class FakeClient:
    """Stands in for a connection to the bridge."""

    def __init__(self) -> None:
        self.is_closed = False
        self.transmitted: list[tuple[int, str]] = []
        self.led: bool | None = None
        self._listeners: list = []

    async def identify(self) -> Identification:
        return Identification(0x155A, 0x100E, "0100")

    async def info(self) -> Info:
        return Info(("RX09 EW+KEELOQ", "www.fuhr.de"))

    async def mode(self) -> int:
        return 0

    async def position_count(self) -> int:
        return 64

    def add_listener(self, callback):
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback)

    async def transmit(self, position: int, key: str) -> None:
        self.transmitted.append((position, key))

    async def set_led(self, on: bool) -> None:
        self.led = on

    async def close(self) -> None:
        self.is_closed = True

    def emit(
        self, key: str, action: Action = Action.PRESS, address: str = WINDOW_ADDRESS
    ):
        """Simulate a telegram arriving from the stick."""
        event = TelegramEvent(address=address, key=key, action=action, rssi=-0x47)
        for listener in list(self._listeners):
            listener(event)


SUBENTRIES = [
    ConfigSubentryData(
        subentry_type=SUBENTRY_COVER,
        title="Living room shutter",
        unique_id=None,
        data={CONF_POSITION: 1, "key_open": "A", "key_close": "B", "key_stop": "C"},
    ),
    ConfigSubentryData(
        subentry_type=SUBENTRY_SWITCH,
        title="Garden socket",
        unique_id=None,
        data={CONF_POSITION: 2, "key_on": "A", "key_off": "B"},
    ),
    ConfigSubentryData(
        subentry_type=SUBENTRY_BUTTON,
        title="Doorbell",
        unique_id=None,
        data={CONF_POSITION: 3, CONF_KEY: "A"},
    ),
    ConfigSubentryData(
        subentry_type=SUBENTRY_CONTACT,
        title="Kitchen window",
        unique_id=f"contact_{WINDOW_ADDRESS}",
        data={
            CONF_ADDRESS: WINDOW_ADDRESS,
            CONF_KEY_STATE_ON: "A",
            CONF_KEY_STATE_OFF: "B",
            "device_class": "window",
        },
    ),
    ConfigSubentryData(
        subentry_type=SUBENTRY_TRANSMITTER,
        title="Hand transmitter",
        unique_id=f"transmitter_{WINDOW_ADDRESS}",
        data={CONF_ADDRESS: WINDOW_ADDRESS},
    ),
]


@pytest.fixture(autouse=True)
def enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load this custom component."""
    return enable_custom_integrations


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
async def loaded_entry(hass: HomeAssistant, fake_client: FakeClient):
    """A fully set up config entry with one device of every kind."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Easywave RX09 EW+KEELOQ",
        unique_id="172.30.32.1:5000",
        data={
            CONF_CONNECTION: CONNECTION_TCP,
            CONF_HOST: "172.30.32.1",
            CONF_PORT: 5000,
        },
        subentries_data=SUBENTRIES,
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.eldat_easywave.hub.connect_tcp",
        AsyncMock(return_value=fake_client),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


class TestSetup:
    async def test_entry_loads(self, hass, loaded_entry):
        assert loaded_entry.state is ConfigEntryState.LOADED
        hub = loaded_entry.runtime_data
        assert hub.position_count == 64
        assert hub.info.fields[0] == "RX09 EW+KEELOQ"
        assert hub.identification.version_string == "1.00"

    async def test_every_subentry_produces_entities(self, hass, loaded_entry):
        states = hass.states.async_entity_ids()
        assert any(e.startswith("cover.") for e in states)
        assert any(e.startswith("switch.") for e in states)
        assert any(e.startswith("button.") for e in states)
        assert any(e.startswith("binary_sensor.") for e in states)
        # One event entity per key code.
        assert len([e for e in states if e.startswith("event.")]) == 4

    async def test_hub_device_is_registered(self, hass, loaded_entry):
        from homeassistant.helpers import device_registry as dr

        registry = dr.async_get(hass)
        device = registry.async_get_device(identifiers={(DOMAIN, "172.30.32.1:5000")})
        assert device is not None
        assert device.sw_version == "1.00"
        assert device.model == "RX09 EW+KEELOQ"

    async def test_unload(self, hass, loaded_entry, fake_client):
        assert await hass.config_entries.async_unload(loaded_entry.entry_id)
        await hass.async_block_till_done()
        assert fake_client.is_closed


class TestTransmitting:
    async def _entity_of(self, hass, platform: Platform) -> str:
        ids = [e for e in hass.states.async_entity_ids() if e.startswith(f"{platform}.")]
        assert ids, f"no {platform} entity"
        return ids[0]

    async def test_cover_sends_configured_keys(self, hass, loaded_entry, fake_client):
        entity_id = await self._entity_of(hass, Platform.COVER)
        await hass.services.async_call(
            "cover", "close_cover", {"entity_id": entity_id}, blocking=True
        )
        assert fake_client.transmitted[-1] == (1, "B")
        assert hass.states.get(entity_id).state == STATE_CLOSED

        await hass.services.async_call(
            "cover", "open_cover", {"entity_id": entity_id}, blocking=True
        )
        assert fake_client.transmitted[-1] == (1, "A")
        assert hass.states.get(entity_id).state == STATE_OPEN

    async def test_cover_state_is_assumed(self, hass, loaded_entry):
        entity_id = await self._entity_of(hass, Platform.COVER)
        assert hass.states.get(entity_id).attributes["assumed_state"] is True

    async def test_cover_stop_clears_state(self, hass, loaded_entry, fake_client):
        """After a stop the shutter is somewhere in between."""
        entity_id = await self._entity_of(hass, Platform.COVER)
        await hass.services.async_call(
            "cover", "close_cover", {"entity_id": entity_id}, blocking=True
        )
        await hass.services.async_call(
            "cover", "stop_cover", {"entity_id": entity_id}, blocking=True
        )
        assert fake_client.transmitted[-1] == (1, "C")
        assert hass.states.get(entity_id).state not in (STATE_OPEN, STATE_CLOSED)

    async def test_switch_on_off(self, hass, loaded_entry, fake_client):
        entity_id = await self._entity_of(hass, Platform.SWITCH)
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": entity_id}, blocking=True
        )
        assert fake_client.transmitted[-1] == (2, "A")
        assert hass.states.get(entity_id).state == STATE_ON

        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": entity_id}, blocking=True
        )
        assert fake_client.transmitted[-1] == (2, "B")
        assert hass.states.get(entity_id).state == STATE_OFF

    async def test_button_press(self, hass, loaded_entry, fake_client):
        entity_id = await self._entity_of(hass, Platform.BUTTON)
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
        assert fake_client.transmitted[-1] == (3, "A")


class TestReceiving:
    def _contact(self, hass) -> str:
        ids = [
            e for e in hass.states.async_entity_ids() if e.startswith("binary_sensor.")
        ]
        assert ids
        return ids[0]

    async def test_window_contact_follows_keys(self, hass, loaded_entry, fake_client):
        """RTS16 in EIN/AUS mode: A on open, B on close."""
        entity_id = self._contact(hass)

        fake_client.emit("A")
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == STATE_ON

        fake_client.emit("B")
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == STATE_OFF

    async def test_contact_reports_rssi(self, hass, loaded_entry, fake_client):
        entity_id = self._contact(hass)
        fake_client.emit("A")
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).attributes["rssi"] == -0x47

    async def test_contact_device_class(self, hass, loaded_entry):
        entity_id = self._contact(hass)
        assert hass.states.get(entity_id).attributes["device_class"] == "window"

    async def test_unrelated_key_leaves_contact_alone(
        self, hass, loaded_entry, fake_client
    ):
        entity_id = self._contact(hass)
        fake_client.emit("A")
        await hass.async_block_till_done()
        fake_client.emit("D")  # a third channel, not part of this contact
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == STATE_ON

    async def test_other_transmitter_is_ignored(self, hass, loaded_entry, fake_client):
        entity_id = self._contact(hass)
        fake_client.emit("B")
        await hass.async_block_till_done()
        fake_client.emit("A", address="deadbeef")
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == STATE_OFF

    async def test_event_entity_records_press(self, hass, loaded_entry, fake_client):
        fake_client.emit("A")
        await hass.async_block_till_done()
        key_a = next(
            e
            for e in hass.states.async_entity_ids()
            if e.startswith("event.")
            and hass.states.get(e).attributes.get("friendly_name", "").endswith("Key A")
        )
        state = hass.states.get(key_a)
        assert state.attributes["event_type"] == str(Action.PRESS)

    async def test_telegram_reaches_the_event_bus(self, hass, loaded_entry, fake_client):
        received = []
        hass.bus.async_listen(f"{DOMAIN}_telegram", received.append)
        fake_client.emit("A")
        await hass.async_block_till_done()
        assert received
        assert received[0].data["address"] == WINDOW_ADDRESS
        assert received[0].data["key"] == "A"

    async def test_hub_remembers_transmitters(self, hass, loaded_entry, fake_client):
        """Feeds the address picker in the config flow."""
        fake_client.emit("A", address="aabbcc")
        await hass.async_block_till_done()
        assert "aabbcc" in loaded_entry.runtime_data.seen


class TestServices:
    async def test_send_telegram(self, hass, loaded_entry, fake_client):
        """The pairing path when the firmware has no RDP? command."""
        await hass.services.async_call(
            DOMAIN, SERVICE_SEND_TELEGRAM, {"position": 7, "key": "C"}, blocking=True
        )
        assert (7, "C") in fake_client.transmitted

    async def test_send_telegram_rejects_bad_key(self, hass, loaded_entry):
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN, SERVICE_SEND_TELEGRAM, {"position": 1, "key": "Z"}, blocking=True
            )

    async def test_set_led(self, hass, loaded_entry, fake_client):
        await hass.services.async_call(DOMAIN, "set_led", {"on": True}, blocking=True)
        assert fake_client.led is True


class TestConfigFlow:
    async def test_user_flow_creates_entry(self, hass, fake_client):
        with patch(
            "custom_components.eldat_easywave.config_flow.connect_tcp",
            AsyncMock(return_value=fake_client),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "user"}
            )
            assert result["type"] == "form"
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_HOST: "172.30.32.1", CONF_PORT: 5000}
            )
        assert result["type"] == "create_entry"
        # The firmware name is far more useful than the USB ids.
        assert result["title"] == "Easywave RX09 EW+KEELOQ"
        assert result["data"] == {
            CONF_CONNECTION: CONNECTION_TCP,
            CONF_HOST: "172.30.32.1",
            CONF_PORT: 5000,
        }

    async def test_user_flow_reports_connection_failure(self, hass):
        from custom_components.eldat_easywave.eldat.protocol import EldatConnectionError

        with patch(
            "custom_components.eldat_easywave.config_flow.connect_tcp",
            AsyncMock(side_effect=EldatConnectionError("refused")),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "user"}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_HOST: "1.2.3.4", CONF_PORT: 5000}
            )
        assert result["type"] == "form"
        assert result["errors"] == {"base": "cannot_connect"}

    async def test_duplicate_bridge_is_rejected(self, hass, loaded_entry, fake_client):
        with patch(
            "custom_components.eldat_easywave.config_flow.connect_tcp",
            AsyncMock(return_value=fake_client),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "user"}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_HOST: "172.30.32.1", CONF_PORT: 5000}
            )
        assert result["type"] == "abort"
        assert result["reason"] == "already_configured"


class TestTransmitterKeySelection:
    """A three-button remote should not get a fourth entity that never fires.

    Reported from real use: the integration created A-D unconditionally, so a
    three-button key fob left one event entity sitting at "unknown" forever.
    """

    async def _setup(self, hass, fake_client, keys):
        data = {CONF_ADDRESS: WINDOW_ADDRESS}
        if keys is not None:
            data["keys"] = keys
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Easywave",
            unique_id="usb:test",
            data={CONF_CONNECTION: CONNECTION_TCP, CONF_HOST: "h", CONF_PORT: 5000},
            subentries_data=[
                ConfigSubentryData(
                    subentry_type=SUBENTRY_TRANSMITTER,
                    title="Key fob",
                    unique_id=None,
                    data=data,
                )
            ],
        )
        entry.add_to_hass(hass)
        with patch(
            "custom_components.eldat_easywave.hub.connect_tcp",
            AsyncMock(return_value=fake_client),
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        return [e for e in hass.states.async_entity_ids() if e.startswith("event.")]

    async def test_three_keys_give_three_entities(self, hass, fake_client):
        assert len(await self._setup(hass, fake_client, ["A", "B", "C"])) == 3

    async def test_one_key_gives_one_entity(self, hass, fake_client):
        """A nurse-call wristband has a single button."""
        assert len(await self._setup(hass, fake_client, ["A"])) == 1

    async def test_all_four_when_all_are_selected(self, hass, fake_client):
        assert len(await self._setup(hass, fake_client, ["A", "B", "C", "D"])) == 4

    async def test_older_entries_without_a_selection_keep_all_four(
        self, hass, fake_client
    ):
        """Subentries created before this option existed must not lose entities."""
        assert len(await self._setup(hass, fake_client, None)) == 4

    async def test_unknown_keys_are_ignored(self, hass, fake_client):
        entities = await self._setup(hass, fake_client, ["A", "Z"])
        assert len(entities) == 1

    async def test_the_selected_key_still_fires(self, hass, fake_client):
        entities = await self._setup(hass, fake_client, ["B"])
        fake_client.emit("B")
        await hass.async_block_till_done()
        assert hass.states.get(entities[0]).state not in (None, "unknown")


class TestDiagnostics:
    """Diagnostics are how a user reports a bug, so they must never be the bug."""

    async def test_reports_the_transceiver_and_its_devices(
        self, hass: HomeAssistant, loaded_entry
    ) -> None:
        data = await async_get_config_entry_diagnostics(hass, loaded_entry)
        assert data["connection"]["available"] is True
        assert data["transceiver"]["usb_ids"] == "155A:100E"
        assert data["transceiver"]["firmware"] == "1.00"
        assert len(data["configured_devices"]) == len(SUBENTRIES)

    async def test_survive_an_unloaded_entry(
        self, hass: HomeAssistant, loaded_entry
    ) -> None:
        # Regression: this raised AttributeError, so the one request that explains
        # a broken entry returned nothing precisely when it was needed most.
        await hass.config_entries.async_unload(loaded_entry.entry_id)
        await hass.async_block_till_done()

        data = await async_get_config_entry_diagnostics(hass, loaded_entry)
        assert data["connection"]["available"] is False
        assert len(data["configured_devices"]) == len(SUBENTRIES)


class TestHeardTransmitters:
    """What the radio heard must outlive a reload, because adding a device
    causes one -- otherwise the list is empty exactly when a second device is
    about to be added from the same session."""

    async def test_survive_a_reload(
        self, hass: HomeAssistant, loaded_entry, fake_client: FakeClient
    ) -> None:
        hub = loaded_entry.runtime_data
        fake_client.emit("A", address="6a563dcd")
        await hass.async_block_till_done()
        assert "6a563dcd" in hub.seen

        with patch(
            "custom_components.eldat_easywave.hub.connect_tcp",
            AsyncMock(return_value=FakeClient()),
        ):
            await hass.config_entries.async_reload(loaded_entry.entry_id)
            await hass.async_block_till_done()

        assert "6a563dcd" in loaded_entry.runtime_data.seen

    async def test_forgotten_when_the_entry_is_removed(
        self, hass: HomeAssistant, loaded_entry, fake_client: FakeClient
    ) -> None:
        fake_client.emit("A", address="6a563dcd")
        await hass.async_block_till_done()

        await hass.config_entries.async_remove(loaded_entry.entry_id)
        await hass.async_block_till_done()

        assert hass.data[DOMAIN]["heard"] == {}
