"""Finding a camera on a shared Wi-Fi network (station mode, see camlink.ble.BleLink.join_network).

On a router or a computer's hotspot the camera gets an address of its own, so it has to be found: it
listens on TCP 7001 there as on its own access point. Every host of the computer's local networks is tried
(64 at a time), and a camera that answers is asked for its identity over the datalink (0x07/0x07, the name
of its own access point) so the right one is used when several cameras share the network.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import socket
import threading
import time

from .datalink import Datalink, poke
from .protocol import ADDR_WIFI, DATALINK_PORT, POKE_PORT
from .util import log
from .wifi import local_ip_towards


HOTSPOT_PREFIX = "192.168.137."   # Windows Mobile Hotspot


def local_addresses() -> list[str]:
    """This computer's private IPv4 addresses (Ethernet, Wi-Fi, a hotspot it runs), most likely first: the
    one the default route uses, then a Windows hotspot, then the rest (virtual machine networks...)."""
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    primary = local_ip_towards("192.0.2.1")          # the address the default route uses (no packet sent)
    found.add(primary)
    usable = []
    for address in found:
        if not address:
            continue
        ip = ipaddress.IPv4Address(address)
        if ip.is_private and not ip.is_loopback and not ip.is_link_local:
            usable.append(address)
    return sorted(usable, key=lambda a: (a != primary, not a.startswith(HOTSPOT_PREFIX), a))


def _open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def candidates(address: str, own: list[str], port: int = POKE_PORT, timeout: float = 0.8) -> list[str]:
    """Hosts of the /24 network around `address` (one of ours) with `port` open."""
    hosts = [str(h) for h in ipaddress.IPv4Network(f"{address}/24", strict=False).hosts() if str(h) not in own]
    with concurrent.futures.ThreadPoolExecutor(64) as pool:
        hits = [h for h, ok in zip(hosts, pool.map(lambda h: _open(h, port, timeout), hosts)) if ok]
    log(f"Network search around {address}: {len(hits)} of {len(hosts)} addresses answered on TCP {port}")
    return hits


def identify(host: str, port: int = DATALINK_PORT, timeout: float = 8.0) -> str | None:
    """Open a short datalink session with `host` and ask its identity: the name of the camera's own access
    point (as read over BLE when it was paired). None when it doesn't answer like a camera."""
    names: list[str] = []
    answered = threading.Event()

    def on_message(m) -> None:
        if m.command == (0x07, 0x07) and m.is_reply and len(m.payload) >= 2 and not m.payload[0]:
            names.append(m.payload[2:2 + m.payload[1]].decode("utf-8", "replace"))
            answered.set()

    def run() -> None:
        try:
            link.run()
        except (RuntimeError, OSError):              # no handshake: not a camera (or not answering)
            pass

    poke(host)
    # handshake only: no registration and no live view request, which would restart the encoder of a
    # camera that is already streaming to this computer
    link = Datalink(host=host, port=port, on_message=on_message, start_live_view=False)
    thread = threading.Thread(target=run, daemon=True, name="lan-identify")
    thread.start()
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline and not answered.is_set() and not link.stopped.is_set():
            if link.ready.is_set():                  # the session is up: ask (again every second)
                try:
                    link.send(0x07, 0x07, b"", ADDR_WIFI)
                except OSError:
                    break
            answered.wait(1.0)
    finally:
        link.stop()
        thread.join(2)
    return names[0] if names else None


def find_camera(identity: str, hint: str | None = None, port: int = DATALINK_PORT,
                skip: set[str] | frozenset[str] = frozenset()) -> str | None:
    """Address of the camera whose identity is `identity` on the local networks, or None. `hint` (where it
    was last time) is tried first; then each local network, the most likely first. Addresses in `skip`
    (cameras this computer is already streaming from) are left alone."""
    own = local_addresses()
    tried: set[str] = set(skip)

    def check(host: str) -> bool:
        tried.add(host)
        name = identify(host, port)
        if name == identity:
            log(f"Found {identity} at {host}")
            return True
        log(f"{host} is " + (f"another camera ({name})" if name else "not a camera that answers"))
        return False

    if hint and hint not in own and hint not in tried and _open(hint, POKE_PORT, 0.8) and check(hint):
        return hint
    for address in own:
        for host in candidates(address, own):
            if host not in tried and check(host):
                return host
    return None
