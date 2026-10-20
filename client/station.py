"""Reaching a camera over a shared Wi-Fi network (station mode)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable

from camlink import lan
from camlink.ble import BleLink
from camlink.registry import CameraRegistry
from camlink.util import log

# After joining, a camera can take about a minute to show up on a router's network.
JOINED_SEARCH = 90.0


async def reach(link: BleLink, registry: CameraRegistry, address: str, network: str, port: int,
                stage: Callable[[str], None], pause: Callable[[float], Awaitable[None]],
                keep_going: Callable[[], bool], in_use: Callable[[], set[str]] = set,
                note: Callable[[str | None], None] = lambda _note: None) -> str:
    """The camera's address on `network`. Look for it there first (it may still be on it, at its last
    address), otherwise ask it over BLE to join, then look again. Raises when it can't be reached.
    `in_use()`: addresses of the other cameras this computer streams from (never probed). `note(text)`:
    progress for the page (join attempts, the search after the join). The searches stop early once
    `keep_going()` is false (they run in threads, which cancelling the caller does not stop)."""
    known = registry.get(address) or {}

    def avoid() -> set[str]:
        # the other cameras this computer streams from, and where the other cameras put on a shared
        # network were last seen: never probed (an identity check would end their own session)
        return set(in_use()) | registry.station_hosts(address)

    stage("lan")
    host = await asyncio.to_thread(lan.find_camera, link.ssid, known.get("host"), port, avoid(), keep_going)
    if host is None:
        password = registry.network_password(network)
        if password is None:
            raise RuntimeError(f"No password saved for “{network}”: set the network again (Cameras)")
        stage("station")
        registry.mark_station(address, True)
        joined = await link.join_network(network, password, progress=note)
        stage("lan")
        started = time.monotonic()
        while time.monotonic() - started < JOINED_SEARCH:   # getting an address can take it a while
            note(("The camera joined" if joined else "No answer from the camera, but it may have joined")
                 + f" “{network}”: looking for it there (it can take a minute)")
            await pause(4)
            if not keep_going():
                break
            if link.disconnected.is_set():
                # it restarted or went away: it is not on the network either, so pair again rather than search
                raise RuntimeError("The camera dropped its Bluetooth link while joining the network; connecting again")
            host = await asyncio.to_thread(lan.find_camera, link.ssid, None, port, avoid(), keep_going)
            if host:
                break
    if host is None:
        raise RuntimeError(f"The camera is not visible on “{network}” from this computer: both must be on the "
                           "same network, and guest networks that keep devices apart don't work")
    registry.mark_station(address, True)
    registry.set_host(address, host)
    return host


async def back_to_own_wifi(link: BleLink | None, registry: CameraRegistry, asked: bool) -> None:
    """A camera that joined a shared network goes back to its own access point (where the phone app and the
    next connection expect it). `asked`: it was put on the network in this BLE session."""
    if link is None or link.disconnected.is_set() or not link.address:
        return
    if asked or (registry.get(link.address) or {}).get("in_station"):
        with contextlib.suppress(Exception):
            if await link.use_own_wifi():
                log(f"{link.name or link.address} back on its own Wi-Fi")
                registry.mark_station(link.address, False)
