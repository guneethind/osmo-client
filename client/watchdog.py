"""Recovering a stalled live view in steps, the cheapest first.

The camera sends a keyframe only when the live view is requested (there is no periodic one), so a lost
packet leaves artefacts until the next request, and a picture that stops coming may need only that. Field
data from the OpenPocketCine project: one live view request fixed some stalls, most needed a fresh link
(new socket and handshake), and repeating the request in a loop blacks the picture out (each one restarts
the encoder). So:

  1. no picture for PICTURE_WAIT s, or a picture arrived incomplete: ask once for a fresh keyframe, never
     again within KEYFRAME_COOLDOWN;
  2. no video packets for `stall` s, or no picture for `stall` + PICTURE_EXTRA s: restart the session.

Nothing is checked while a pause is expected (format change, gallery): see App.video_grace.
"""

from __future__ import annotations

PICTURE_WAIT = 2.0
KEYFRAME_COOLDOWN = 6.0
PICTURE_EXTRA = 3.0


class FeedWatchdog:
    def __init__(self, stall: float, started: float) -> None:
        self.stall = stall
        self.started = started
        self._last_keyframe = 0.0
        self._truncated = 0
        self.keyframes = 0                 # keyframe requests made in this session

    def check(self, now: float, last_packet: float, last_picture: float, grace: float, truncated: int) -> str | None:
        """'keyframe', 'restart' or None. Times are time.monotonic() values (0 = never)."""
        incomplete = truncated > self._truncated
        self._truncated = truncated
        if now < grace:
            return None
        packets_age = now - max(last_packet, self.started, grace)
        picture_age = now - max(last_picture, self.started, grace)
        if packets_age > self.stall or picture_age > self.stall + PICTURE_EXTRA:
            return "restart"
        if (picture_age > PICTURE_WAIT or incomplete) and now - self._last_keyframe > KEYFRAME_COOLDOWN:
            self._last_keyframe = now
            self.keyframes += 1
            return "keyframe"
        return None
