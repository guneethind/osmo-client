"""Recovering a stalled live view in steps, the cheapest first.

The camera sends a keyframe only when the live view is requested (there is no periodic one), so a lost
packet leaves artefacts until the next request, and a picture that stops coming may need only that. Field
data from the OpenPocketCine project: one live view request fixed some stalls, most needed a fresh link
(new socket and handshake), and repeating the request in a loop blacks the picture out (each one restarts
the encoder). So:

  1. no picture for PICTURE_WAIT s, or a picture arrived incomplete: ask once for a fresh keyframe, never
     again within KEYFRAME_COOLDOWN;
  2. no video packets for `stall` s, or no picture for `stall` + PICTURE_EXTRA s: restart the session.

Before the first video packet of a session neither applies: the encoder is starting (OpenPocketCine gives
it 8 s after the live view request; an Osmo Pocket over a router can take far longer), and asking again
would restart it. The session restarts after `start_wait` s without any, a wait the caller lengthens
for a camera whose previous sessions got none (see start_wait()). The wait for the first picture counts
from the first packet.

Nothing is checked while a pause is expected (format change, gallery): see App.video_grace.
"""

from __future__ import annotations

PICTURE_WAIT = 2.0
KEYFRAME_COOLDOWN = 6.0
PICTURE_EXTRA = 3.0
START_WAIT = 8.0
START_WAIT_MAX = 60.0


def start_wait(sessions_without_video: int) -> float:
    """How long a session waits for its first video: doubled after each session in a row that got none."""
    return min(START_WAIT * 2 ** min(sessions_without_video, 8), START_WAIT_MAX)


class FeedWatchdog:
    def __init__(self, stall: float, started: float, start_wait: float = START_WAIT) -> None:
        self.stall = stall
        self.started = started
        self.start_wait = start_wait
        self._video_from = 0.0             # when the first video packet of the session was seen
        self._last_keyframe = 0.0
        self._truncated = 0
        self.keyframes = 0                 # keyframe requests made in this session

    def check(self, now: float, last_packet: float, last_picture: float, grace: float, truncated: int) -> str | None:
        """'keyframe', 'restart' or None. Times are time.monotonic() values (0 = never)."""
        incomplete = truncated > self._truncated
        self._truncated = truncated
        if now < grace:
            return None
        if not last_packet:                # no video yet in this session
            return "restart" if now - max(self.started, grace) > self.start_wait else None
        self._video_from = self._video_from or last_packet
        packets_age = now - max(last_packet, self.started, grace)
        picture_age = now - max(last_picture, self.started, grace, self._video_from)
        if packets_age > self.stall or picture_age > self.stall + PICTURE_EXTRA:
            return "restart"
        if (picture_age > PICTURE_WAIT or incomplete) and now - self._last_keyframe > KEYFRAME_COOLDOWN:
            self._last_keyframe = now
            self.keyframes += 1
            return "keyframe"
        return None
