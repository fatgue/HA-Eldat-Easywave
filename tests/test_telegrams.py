"""Burst collapsing tests.

Timings mirror a real capture from an ELDAT RTS16 window contact: five frames
about 38 ms apart per contact change.
"""

from __future__ import annotations

import pytest

from custom_components.eldat_easywave.eldat.parser import Received
from custom_components.eldat_easywave.eldat.telegrams import (
    Action,
    BurstCollapser,
    TelegramEvent,
)

MEASURED_FRAME_GAP = 0.038
MEASURED_BURST_LENGTH = 5


class Clock:
    """Deterministic monotonic clock, so tests never sleep."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def collapser(clock: Clock) -> BurstCollapser:
    return BurstCollapser(time_source=clock)


def telegram(key: str = "B", address: str = "1a2b3c4d", rssi: int = -0x47) -> Received:
    return Received(address=address, key=key, rssi=rssi, channel=0)


def feed_burst(
    collapser: BurstCollapser,
    clock: Clock,
    key: str = "B",
    *,
    frames: int = MEASURED_BURST_LENGTH,
    gap: float = MEASURED_FRAME_GAP,
    address: str = "1a2b3c4d",
) -> list[TelegramEvent]:
    events: list[TelegramEvent] = []
    for index in range(frames):
        if index:
            clock.advance(gap)
        events.extend(collapser.feed(telegram(key=key, address=address)))
    return events


class TestBurstCollapsing:
    def test_five_frame_burst_is_one_press(self, collapser, clock):
        """The whole point: one physical action must not become five events."""
        events = feed_burst(collapser, clock)
        assert len(events) == 1
        assert events[0].action is Action.PRESS
        assert events[0].key == "B"
        assert events[0].address == "1a2b3c4d"
        assert events[0].rssi == -0x47

    def test_plain_press_emits_no_release(self, collapser, clock):
        """A short press is fully described by its PRESS event."""
        feed_burst(collapser, clock)
        clock.advance(1.0)
        assert collapser.tick() == []

    def test_two_presses_are_two_events(self, collapser, clock):
        first = feed_burst(collapser, clock)
        clock.advance(1.0)
        collapser.tick()
        second = feed_burst(collapser, clock)
        assert len(first) == 1 and len(second) == 1

    def test_spec_gap_would_have_split_the_burst(self, collapser, clock):
        """Guards the reason the window is measured, not taken from the spec.

        The specification says repeats come "at least every 100 ms". Had the gap
        been sized below the real 38 ms cadence, each burst would fragment.
        """
        assert collapser.gap > MEASURED_FRAME_GAP * MEASURED_BURST_LENGTH


class TestWindowSensorSemantics:
    def test_open_then_close_are_distinct_events(self, collapser, clock):
        """RTS16E5001B01: opening sends code A, closing sends code B."""
        opened = feed_burst(collapser, clock, key="A")
        clock.advance(1.0)
        collapser.tick()
        closed = feed_burst(collapser, clock, key="B")

        assert [event.key for event in opened] == ["A"]
        assert [event.key for event in closed] == ["B"]

    def test_different_keys_do_not_collapse_together(self, collapser, clock):
        """A and B are separate streams even without a gap between them."""
        events = feed_burst(collapser, clock, key="A")
        events += feed_burst(collapser, clock, key="B")
        assert [(e.key, e.action) for e in events] == [
            ("A", Action.PRESS),
            ("B", Action.PRESS),
        ]

    def test_two_transmitters_are_independent(self, collapser, clock):
        events = feed_burst(collapser, clock, address="1a2b3c4d")
        events += feed_burst(collapser, clock, address="1c14a3")
        assert {event.address for event in events} == {"1a2b3c4d", "1c14a3"}


class TestHoldDetection:
    def test_long_burst_yields_press_then_hold(self, collapser, clock):
        events = feed_burst(collapser, clock, frames=30)
        actions = [event.action for event in events]
        assert actions[0] is Action.PRESS
        assert Action.HOLD in actions

    def test_hold_is_emitted_only_once(self, collapser, clock):
        events = feed_burst(collapser, clock, frames=60)
        assert [e.action for e in events].count(Action.HOLD) == 1

    def test_release_follows_a_hold(self, collapser, clock):
        feed_burst(collapser, clock, frames=30)
        clock.advance(1.0)
        released = collapser.tick()
        assert [event.action for event in released] == [Action.RELEASE]

    def test_release_reports_repeat_count(self, collapser, clock):
        feed_burst(collapser, clock, frames=30)
        clock.advance(1.0)
        assert collapser.tick()[0].repeats == 30

    def test_state_is_cleared_after_release(self, collapser, clock):
        feed_burst(collapser, clock, frames=30)
        clock.advance(1.0)
        collapser.tick()
        assert collapser.tick() == []
        # A later burst starts fresh rather than continuing the old one.
        assert feed_burst(collapser, clock)[0].action is Action.PRESS


class TestTickBehaviour:
    def test_tick_without_input_is_harmless(self, collapser):
        assert collapser.tick() == []

    def test_release_also_surfaces_via_next_telegram(self, collapser, clock):
        """Releases must not depend solely on tick() being called."""
        feed_burst(collapser, clock, frames=30, key="A")
        clock.advance(1.0)
        events = collapser.feed(telegram(key="B"))
        assert Action.RELEASE in [event.action for event in events]


class TestRealCaptureReplay:
    """Replays a genuine RTS16 recording through the collapser.

    The numbers below are properties of real hardware, not of this code, which is
    what makes them worth asserting: if the collapse window ever drifts away from
    the measured 38 ms cadence, this fails.
    """

    @pytest.fixture
    def replayed(self, clock):
        from custom_components.eldat_easywave.eldat.parser import decode_payload

        from .capture_rts16 import CAPTURE

        collapser = BurstCollapser(time_source=clock)
        clock.now = CAPTURE[0][0]
        events: list[TelegramEvent] = []
        for timestamp, frame in CAPTURE:
            clock.now = timestamp
            message = decode_payload(frame)
            assert isinstance(message, Received), f"failed to decode {frame!r}"
            events.extend(collapser.feed(message))
        events.extend(collapser.tick())
        return CAPTURE, events

    def test_every_frame_decodes(self, replayed):
        capture, _ = replayed
        assert len(capture) == 65

    def test_bursts_collapse_to_one_event_each(self, replayed):
        """65 frames on the wire were 13 physical contact changes."""
        capture, events = replayed
        assert len(capture) == 65
        assert len(events) == 13

    def test_no_frame_is_left_stranded(self, replayed):
        """Five frames per change, so the ratio must come out exactly."""
        capture, events = replayed
        assert len(capture) / len(events) == 5.0

    def test_all_events_are_presses(self, replayed):
        """A contact change is momentary -- never a hold."""
        _, events = replayed
        assert {event.action for event in events} == {Action.PRESS}

    def test_keys_alternate_open_and_closed(self, replayed):
        """Opening sends A, closing sends B, so they must alternate."""
        _, events = replayed
        keys = [event.key for event in events]
        assert set(keys) == {"A", "B"}
        assert all(first != second for first, second in zip(keys, keys[1:], strict=False))

    def test_single_transmitter(self, replayed):
        _, events = replayed
        assert {event.address for event in events} == {"1a2b3c4d"}

    def test_rssi_decodes_into_a_plausible_range(self, replayed):
        """Proof the signed-hex parsing is right: decimal parsing would fail."""
        _, events = replayed
        values = [event.rssi for event in events]
        assert all(-100 < value < -30 for value in values), values
