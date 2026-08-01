"""Turning telegram bursts into logical events.

An Easywave transmitter does not send one telegram per action -- it sends a short
burst. Measured on an ELDAT RTS16 window contact via an RX09 transceiver, a
single contact change produces exactly five identical frames roughly 38 ms
apart:

    +51.827s  REC00,-47,1A2B3C4D,B
    +51.865s  REC00,-46,1A2B3C4D,B
    +51.903s  REC00,-47,1A2B3C4D,B
    +51.941s  REC00,-46,1A2B3C4D,B
    +51.978s  REC00,-47,1A2B3C4D,B

Note that the specification claims repeats arrive "at least every 100 ms", so
sizing the collapse window from the document rather than from the wire would
have split every burst into five separate events.

Consumers want one event per physical action, so bursts are collapsed here
rather than in every entity.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from .parser import Received

#: Silence after which a burst is considered finished. Comfortably above the
#: measured ~38 ms inter-frame gap, well below a plausible double-press.
DEFAULT_BURST_GAP: Final = 0.4

#: A burst still going after this long is treated as a held key rather than a
#: single press. A normal five-frame burst spans ~150 ms, so this does not fire
#: for ordinary presses.
DEFAULT_HOLD_AFTER: Final = 0.6


class Action(StrEnum):
    """What a transmitter did, derived from the burst shape."""

    PRESS = "press"
    HOLD = "hold"
    RELEASE = "release"


@dataclass(frozen=True, slots=True)
class TelegramEvent:
    """One logical action from one transmitter key."""

    address: str
    key: str
    action: Action
    rssi: int | None = None
    repeats: int = 1


@dataclass(slots=True)
class _Burst:
    started: float
    last_seen: float
    repeats: int
    rssi: int | None
    hold_sent: bool = False


@dataclass
class BurstCollapser:
    """Collapses repeated telegrams into press / hold / release events.

    Stateful and not thread-safe; drive it from a single event loop.

    ``time_source`` is injectable so tests can drive timing deterministically
    instead of sleeping.
    """

    gap: float = DEFAULT_BURST_GAP
    hold_after: float = DEFAULT_HOLD_AFTER
    time_source: Callable[[], float] = time.monotonic
    _bursts: dict[tuple[str, str], _Burst] = field(default_factory=dict, init=False)

    def feed(self, telegram: Received) -> list[TelegramEvent]:
        """Absorb one received telegram, returning any events it produced."""
        now = self.time_source()
        events = self._expire(now)
        identity = (telegram.address, telegram.key)
        burst = self._bursts.get(identity)

        if burst is None:
            self._bursts[identity] = _Burst(
                started=now, last_seen=now, repeats=1, rssi=telegram.rssi
            )
            events.append(
                TelegramEvent(
                    address=telegram.address,
                    key=telegram.key,
                    action=Action.PRESS,
                    rssi=telegram.rssi,
                    repeats=1,
                )
            )
            return events

        burst.last_seen = now
        burst.repeats += 1
        if telegram.rssi is not None:
            burst.rssi = telegram.rssi

        if not burst.hold_sent and now - burst.started >= self.hold_after:
            burst.hold_sent = True
            events.append(
                TelegramEvent(
                    address=telegram.address,
                    key=telegram.key,
                    action=Action.HOLD,
                    rssi=burst.rssi,
                    repeats=burst.repeats,
                )
            )
        return events

    def tick(self) -> list[TelegramEvent]:
        """Emit pending release events without new input.

        Call periodically, otherwise a release is only noticed on the next
        telegram from any transmitter.
        """
        return self._expire(self.time_source())

    def _expire(self, now: float) -> list[TelegramEvent]:
        events: list[TelegramEvent] = []
        for identity, burst in list(self._bursts.items()):
            if now - burst.last_seen < self.gap:
                continue
            del self._bursts[identity]
            # Only a held key has a meaningful release; a plain press is
            # already fully described by its PRESS event.
            if burst.hold_sent:
                address, key = identity
                events.append(
                    TelegramEvent(
                        address=address,
                        key=key,
                        action=Action.RELEASE,
                        rssi=burst.rssi,
                        repeats=burst.repeats,
                    )
                )
        return events
