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
from collections.abc import Callable

from .datalink import Datalink, poke
from .protocol import ADDR_WIFI, CAMERA_HOST, DATALINK_PORT, POKE_PORT
from .util import log
from .wifi import local_ip_towards


HOTSPOT_PREFIX = "192.168.137."   # Windows Mobile Hotspot
# More hosts than this answering on TCP 7001 is not cameras: a virtual adapter (WSL, Hyper-V) or software
# that accepts every connection. Such a network is skipped rather than asking each host who it is.
MAX_CANDIDATES = 8


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
                skip: set[str] | frozenset[str] = frozenset(),
                keep_going: Callable[[], bool] = lambda: True) -> str | None:
    """Address of the camera whose identity is `identity` on the local networks, or None. `hint` (where it
    was last time) is tried first; then each local network, the most likely first, its hosts asked all at
    once. Addresses in `skip` (cameras this computer is already streaming from) are left alone, and so is
    a camera's own access point (CAMERA_HOST): a computer still on the camera's Wi-Fi finds it there, but it
    has not joined the shared network (on a router's network the router itself holds that address).
    Gives up between networks once `keep_going()` is false."""
    own = local_addresses()
    tried: set[str] = set(skip)

    def check(hosts: list[str]) -> str | None:
        tried.update(hosts)
        with concurrent.futures.ThreadPoolExecutor(len(hosts)) as pool:
            names = list(pool.map(lambda h: identify(h, port), hosts))
        for host, name in zip(hosts, names):
            if name == identity:
                log(f"Found {identity} at {host}")
                return host
            log(f"{host} is " + (f"another camera ({name})" if name else "not a camera that answers"))
        return None

    if hint and hint != CAMERA_HOST and hint not in own and hint not in tried:
        if _open(hint, POKE_PORT, 0.8) and check([hint]):
            return hint
    for address in own:
        if not keep_going():
            return None
        hosts = candidates(address, own)
        if CAMERA_HOST in hosts:
            log(f"{CAMERA_HOST} is a camera's own access point, not the shared network")
        hosts = [h for h in hosts if h != CAMERA_HOST and h not in tried]
        if len(hosts) > MAX_CANDIDATES:
            log(f"Skipped the network around {address}: {len(hosts)} addresses answer, so they are not cameras "
                "(a virtual adapter, or software that accepts every connection)")
            continue
        if hosts and keep_going():
            found = check(hosts)
            if found:
                return found
    return None
