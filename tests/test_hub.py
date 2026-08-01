"""Connection supervision.

The bridge add-on can restart at any time -- an update, a stick replug -- so
recovering from a dropped connection without user intervention matters more than
almost anything else here.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.eldat_easywave.eldat.parser import Identification, Info
from custom_components.eldat_easywave.eldat.protocol import EldatConnectionError
from custom_components.eldat_easywave.hub import EldatHub


class StubClient:
    def __init__(self) -> None:
        self.is_closed = False
        self._listeners: list = []

    async def identify(self):
        return Identification(0x155A, 0x100E, "0100")

    async def info(self):
        return Info(("RX09 EW+KEELOQ", "www.fuhr.de"))

    async def mode(self):
        return 0

    async def position_count(self):
        return 64

    def add_listener(self, callback):
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback)

    async def close(self):
        self.is_closed = True


@pytest.fixture
def hub(hass: HomeAssistant) -> EldatHub:
    return EldatHub(hass, "entry-1", "172.30.32.1", 5000)


class TestSetup:
    async def test_collects_transceiver_details(self, hass, hub):
        with patch(
            "custom_components.eldat_easywave.hub.connect_tcp",
            AsyncMock(return_value=StubClient()),
        ):
            await hub.async_setup()
        try:
            assert hub.available
            assert hub.position_count == 64
            assert hub.mode == 0
            assert hub.info.fields[0] == "RX09 EW+KEELOQ"
        finally:
            await hub.async_shutdown()

    async def test_failure_propagates(self, hass, hub):
        """So Home Assistant can raise ConfigEntryNotReady and retry."""
        with (
            patch(
                "custom_components.eldat_easywave.hub.connect_tcp",
                AsyncMock(side_effect=EldatConnectionError("refused")),
            ),
            pytest.raises(EldatConnectionError),
        ):
            await hub.async_setup()

    async def test_unique_id_is_the_bridge_endpoint(self, hub):
        """The stick's USB serial is not reachable over the protocol."""
        assert hub.unique_id == "172.30.32.1:5000"


class TestSupervision:
    @pytest.mark.parametrize(
        "failure",
        [
            EldatConnectionError("bridge is down"),
            RuntimeError("something unmodelled"),
            OSError("host unreachable"),
        ],
    )
    async def test_reconnect_swallows_every_failure(self, hass, hub, failure):
        """An exception escaping here would kill the supervisor for good."""
        with patch(
            "custom_components.eldat_easywave.hub.connect_tcp",
            AsyncMock(return_value=StubClient()),
        ):
            await hub.async_setup()
        try:
            with patch(
                "custom_components.eldat_easywave.hub.connect_tcp",
                AsyncMock(side_effect=failure),
            ):
                assert await hub._reconnect_once() is False
            assert not hub.available
        finally:
            await hub.async_shutdown()

    async def test_reconnect_restores_availability(self, hass, hub):
        # A fresh client per call, as a real reconnect would get -- reusing one
        # instance would just hand back the connection _close_client() shut.
        with patch(
            "custom_components.eldat_easywave.hub.connect_tcp",
            AsyncMock(side_effect=lambda *args, **kwargs: StubClient()),
        ):
            await hub.async_setup()
            try:
                assert await hub._reconnect_once() is True
                assert hub.available
            finally:
                await hub.async_shutdown()

    async def test_supervisor_survives_a_failed_reconnect(self, hass, hub):
        """End to end through the real loop, with the retry delay shortened."""
        client = StubClient()
        with (
            patch(
                "custom_components.eldat_easywave.hub.connect_tcp",
                AsyncMock(return_value=client),
            ),
            patch("custom_components.eldat_easywave.hub._RECONNECT_DELAYS", (0,)),
            patch("custom_components.eldat_easywave.hub._POLL_INTERVAL", 0),
        ):
            await hub.async_setup()
            try:
                client.is_closed = True  # the bridge went away
                with patch(
                    "custom_components.eldat_easywave.hub.connect_tcp",
                    AsyncMock(side_effect=RuntimeError("unmodelled")),
                ):
                    await asyncio.sleep(0.05)
                assert not hub._supervisor.done()
            finally:
                await hub.async_shutdown()

    async def test_shutdown_closes_the_client(self, hass, hub):
        client = StubClient()
        with patch(
            "custom_components.eldat_easywave.hub.connect_tcp",
            AsyncMock(return_value=client),
        ):
            await hub.async_setup()
        await hub.async_shutdown()
        assert client.is_closed
        assert not hub.available

    async def test_shutdown_is_idempotent(self, hass, hub):
        with patch(
            "custom_components.eldat_easywave.hub.connect_tcp",
            AsyncMock(return_value=StubClient()),
        ):
            await hub.async_setup()
        await hub.async_shutdown()
        await hub.async_shutdown()


class TestSeenTransmitters:
    async def test_newest_transmitter_is_last(self, hass, hub):
        """The config flow reverses this to show the newest first."""
        from custom_components.eldat_easywave.eldat.telegrams import (
            Action,
            TelegramEvent,
        )

        client = StubClient()
        with patch(
            "custom_components.eldat_easywave.hub.connect_tcp",
            AsyncMock(return_value=client),
        ):
            await hub.async_setup()
        try:
            for address in ("aaaaaa", "bbbbbb", "aaaaaa"):
                hub._handle_telegram(
                    TelegramEvent(address=address, key="A", action=Action.PRESS)
                )
            # "aaaaaa" was heard again, so it must have moved to the end.
            assert list(hub.seen) == ["bbbbbb", "aaaaaa"]
        finally:
            await hub.async_shutdown()
