"""Optional P2P integrations. Everything here is best-effort and guarded:

* mDNS advertisement/discovery via the third-party ``zeroconf`` package.
* UPnP port mapping via the third-party ``miniupnpc`` package.

Neither is a dependency of Jade. When the package is absent, advertise/
discover functions return ``None`` (or an honest failure dict for UPnP) and
nothing raises. Tests must skip cleanly when the libraries are missing —
use ``pytest.importorskip("zeroconf")`` / ``pytest.importorskip("miniupnpc")``
for the live paths.
"""
import socket
import time

MDNS_SERVICE_TYPE = "_jade-p2p._tcp.local."
MDNS_NAME_PREFIX = "Jade-"


# ---------------------------------------------------------------------------
# mDNS (zeroconf)
# ---------------------------------------------------------------------------

def _local_ip():
    try:
        from tools.network import get_local_ip
        return get_local_ip()
    except Exception:
        return "127.0.0.1"


def mdns_advertise(port=11435, name=None):
    """Advertise this Jade node on the LAN via mDNS.

    Returns a handle with a ``close()`` method, or ``None`` when
    ``zeroconf`` is not installed. The TXT record carries node_id,
    version and the human name — capability *names* only, never keys.
    """
    try:
        from zeroconf import Zeroconf, ServiceInfo
    except ImportError:
        return None
    from core.update import CURRENT_VERSION
    from tools.hardware import get_node_id
    from core.config import load_config

    node_id = get_node_id()
    svc_name = (name or load_config().get("jade_name", "Jade") or
                "Jade")
    # mDNS service names must be unique on the link; suffix with node id.
    reg_name = f"{MDNS_NAME_PREFIX}{svc_name}-{node_id[:6]}.{MDNS_SERVICE_TYPE}"
    try:
        addr = socket.inet_aton(_local_ip())
    except OSError:
        addr = socket.inet_aton("127.0.0.1")
    info = ServiceInfo(
        MDNS_SERVICE_TYPE, reg_name,
        addresses=[addr], port=port,
        properties={b"node_id": node_id.encode(),
                    b"version": CURRENT_VERSION.encode(),
                    b"name": svc_name.encode()[:64]},
    )
    zc = Zeroconf()
    zc.register_service(info)

    class _Handle:
        def close(self):
            try:
                zc.unregister_service(info)
            finally:
                zc.close()

    return _Handle()


def mdns_discover(timeout=3.0):
    """Browse for Jade mDNS advertisements on the LAN.

    Returns a list of ``{"name", "host", "port", "node_id", "version"}``,
    or ``None`` when ``zeroconf`` is not installed.
    """
    try:
        from zeroconf import Zeroconf, ServiceBrowser
    except ImportError:
        return None

    found = {}

    class _Listener:
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=1500)
            if info is None:
                return
            props = {}
            try:
                raw = info.properties or {}
                props = {k.decode(errors="replace"): v.decode(errors="replace")
                         for k, v in raw.items()}
            except Exception:
                pass
            addrs = []
            try:
                addrs = [socket.inet_ntoa(a) for a in (info.addresses or [])]
            except Exception:
                pass
            if not addrs:
                return
            found[name] = {
                "name": props.get("name", name),
                "host": addrs[0],
                "port": info.port,
                "node_id": props.get("node_id", ""),
                "version": props.get("version", "unknown"),
            }

        def remove_service(self, zc, type_, name):
            found.pop(name, None)

        def update_service(self, zc, type_, name):
            self.add_service(zc, type_, name)

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, MDNS_SERVICE_TYPE, _Listener())
        time.sleep(timeout)
    finally:
        zc.close()
    return list(found.values())


# ---------------------------------------------------------------------------
# UPnP (miniupnpc)
# ---------------------------------------------------------------------------

def p2p_upnp_map(port=11435, protocol="TCP", lease_seconds=3600):
    """Attempt to map ``port`` on the LAN gateway via UPnP IGD.

    Returns ``{"ok", "external_ip", "error"}`` — always a dict, never an
    exception for the expected failure modes (no library, no gateway,
    gateway refused). ``external_ip`` is the gateway's public address as
    reported by the IGD, or ``None`` when unknown.
    """
    try:
        import miniupnpc
    except ImportError:
        return {"ok": False, "external_ip": None,
                "error": "miniupnpc not installed; UPnP mapping unavailable"}
    try:
        upnp = miniupnpc.UPnP()
        upnp.discoverdelay = 200
        if upnp.discover() == 0:
            return {"ok": False, "external_ip": None,
                    "error": "no UPnP gateway discovered on the LAN"}
        upnp.selectigd()
        try:
            external = upnp.externalipaddress()
        except Exception:
            external = None
        try:
            lan_addr = upnp.lanaddr()
        except Exception:
            lan_addr = _local_ip()
        mapped = upnp.addportmapping(port, protocol, lan_addr, port,
                                     "Jade P2P", "")
        if mapped:
            return {"ok": True, "external_ip": external, "error": None}
        return {"ok": False, "external_ip": external,
                "error": f"gateway refused {protocol} port mapping for {port}"}
    except Exception as e:
        return {"ok": False, "external_ip": None,
                "error": f"UPnP failed: {e}"}


def p2p_upnp_unmap(port=11435, protocol="TCP"):
    """Remove a UPnP mapping previously created by :func:`p2p_upnp_map`.

    Returns ``{"ok", "error"}``; honest about a missing library or gateway.
    """
    try:
        import miniupnpc
    except ImportError:
        return {"ok": False,
                "error": "miniupnpc not installed; nothing to unmap"}
    try:
        upnp = miniupnpc.UPnP()
        upnp.discoverdelay = 200
        if upnp.discover() == 0:
            return {"ok": False, "error": "no UPnP gateway discovered"}
        upnp.selectigd()
        ok = upnp.deleteportmapping(port, protocol)
        return {"ok": bool(ok),
                "error": None if ok else "gateway refused to delete mapping"}
    except Exception as e:
        return {"ok": False, "error": f"UPnP unmap failed: {e}"}
