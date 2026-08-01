"""A real capture from an ELDAT RTS16E5001B01 window contact.

Recorded over a userspace CP210x connection to an ELDAT transceiver
(155A:100E, "RX09 EW+KEELOQ"), while the contact was opened and closed
repeatedly. Timestamps are seconds since the start of the capture.

Each ``(timestamp, frame)`` pair is one telegram as it came off the wire, with one
change: the transmitter address has been replaced with the synthetic ``1A2B3C4D``
so a real device identifier from a private home does not live in a public
repository. Timings, RSSI values, key codes and frame count are untouched -- those
are what the tests assert.
Replaying this through the real burst collapser is the closest thing to a
hardware test that can run in CI.
"""

from __future__ import annotations

#: (seconds, frame payload) as captured.
CAPTURE = [
    (61.142, "REC00,-45,1A2B3C4D,A"),
    (61.180, "REC00,-44,1A2B3C4D,A"),
    (61.218, "REC00,-45,1A2B3C4D,A"),
    (61.255, "REC00,-42,1A2B3C4D,A"),
    (61.293, "REC00,-42,1A2B3C4D,A"),
    (61.466, "REC00,-42,1A2B3C4D,B"),
    (61.503, "REC00,-41,1A2B3C4D,B"),
    (61.541, "REC00,-41,1A2B3C4D,B"),
    (61.579, "REC00,-42,1A2B3C4D,B"),
    (61.616, "REC00,-43,1A2B3C4D,B"),
    (61.789, "REC00,-45,1A2B3C4D,A"),
    (61.826, "REC00,-46,1A2B3C4D,A"),
    (61.864, "REC00,-46,1A2B3C4D,A"),
    (61.902, "REC00,-46,1A2B3C4D,A"),
    (61.940, "REC00,-46,1A2B3C4D,A"),
    (64.829, "REC00,-4B,1A2B3C4D,B"),
    (64.867, "REC00,-4C,1A2B3C4D,B"),
    (64.905, "REC00,-4C,1A2B3C4D,B"),
    (64.942, "REC00,-4C,1A2B3C4D,B"),
    (64.981, "REC00,-4C,1A2B3C4D,B"),
    (65.202, "REC00,-4B,1A2B3C4D,A"),
    (65.240, "REC00,-4A,1A2B3C4D,A"),
    (65.279, "REC00,-4B,1A2B3C4D,A"),
    (65.316, "REC00,-4A,1A2B3C4D,A"),
    (65.353, "REC00,-4A,1A2B3C4D,A"),
    (116.666, "REC00,-48,1A2B3C4D,B"),
    (116.704, "REC00,-48,1A2B3C4D,B"),
    (116.741, "REC00,-49,1A2B3C4D,B"),
    (116.779, "REC00,-49,1A2B3C4D,B"),
    (116.817, "REC00,-4A,1A2B3C4D,B"),
    (118.712, "REC00,-46,1A2B3C4D,A"),
    (118.750, "REC00,-45,1A2B3C4D,A"),
    (118.788, "REC00,-45,1A2B3C4D,A"),
    (118.826, "REC00,-45,1A2B3C4D,A"),
    (118.863, "REC00,-45,1A2B3C4D,A"),
    (120.166, "REC00,-43,1A2B3C4D,B"),
    (120.204, "REC00,-43,1A2B3C4D,B"),
    (120.243, "REC00,-43,1A2B3C4D,B"),
    (120.279, "REC00,-43,1A2B3C4D,B"),
    (120.317, "REC00,-44,1A2B3C4D,B"),
    (121.842, "REC00,-55,1A2B3C4D,A"),
    (121.880, "REC00,-52,1A2B3C4D,A"),
    (121.918, "REC00,-52,1A2B3C4D,A"),
    (121.956, "REC00,-52,1A2B3C4D,A"),
    (121.995, "REC00,-52,1A2B3C4D,A"),
    (122.482, "REC00,-44,1A2B3C4D,B"),
    (122.519, "REC00,-44,1A2B3C4D,B"),
    (122.557, "REC00,-44,1A2B3C4D,B"),
    (122.595, "REC00,-44,1A2B3C4D,B"),
    (122.633, "REC00,-44,1A2B3C4D,B"),
    (123.752, "REC00,-55,1A2B3C4D,A"),
    (123.790, "REC00,-54,1A2B3C4D,A"),
    (123.827, "REC00,-54,1A2B3C4D,A"),
    (123.865, "REC00,-54,1A2B3C4D,A"),
    (123.902, "REC00,-54,1A2B3C4D,A"),
    (124.908, "REC00,-45,1A2B3C4D,B"),
    (124.945, "REC00,-44,1A2B3C4D,B"),
    (124.983, "REC00,-45,1A2B3C4D,B"),
    (125.021, "REC00,-45,1A2B3C4D,B"),
    (125.059, "REC00,-44,1A2B3C4D,B"),
    (129.524, "REC00,-49,1A2B3C4D,A"),
    (129.561, "REC00,-4B,1A2B3C4D,A"),
    (129.599, "REC00,-49,1A2B3C4D,A"),
    (129.636, "REC00,-49,1A2B3C4D,A"),
    (129.674, "REC00,-4D,1A2B3C4D,A"),
]
