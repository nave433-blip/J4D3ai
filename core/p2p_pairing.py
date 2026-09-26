"""LAN-first pairing for Cortana P2P.

A pairing code is a short, copy-pasteable string that carries the peer's
shared P2P token plus its identity::

    cortana p2p-pair          # on the machine you want to connect TO
    cortana p2p-join <code>   # on the machine connecting FROM

Honest limits (no fake "cloud pairing"):
* The code does NOT traverse NAT by itself. Join works when both machines
  are on the same LAN: the joiner finds the peer with UDP broadcast
  discovery and matches it by node_id, then stores the shared token
  locally so it can authenticate to the peer.
* If the peer is not on the LAN, join tells you exactly what is needed:
  forward TCP 11435 to the peer and set ``p2p_public_endpoint`` on it
  (or use a self-hosted discovery server so the joiner can find it).

Stdlib only. QR rendering uses the optional third-party ``qrcode``
package when installed, and silently falls back to the plain string.
"""
import base64
import hashlib
import json
import secrets

PAIRING_PREFIX = "CP1-"
PAIRING_VERSION = 1


def _local_endpoint(port=11435):
    try:
        from tools.network import get_local_ip
        return f"{get_local_ip()}:{port}"
    except Exception:
        return f"127.0.0.1:{port}"


def _token_fingerprint(token):
    return hashlib.sha256(str(token).encode()).hexdigest()[:12]


def make_pairing_code(port=11435):
    """Build a pairing code for this node.

    Returns (code, info) where info has token_fingerprint, endpoint,
    node_id and whether a fresh token was generated. If no p2p_token is
    configured yet, a strong random one is generated and saved — pairing
    is precisely the act of establishing the shared secret.
    """
    from core.config import load_config, save_config
    from tools.hardware import get_node_id
    cfg = load_config()
    token = cfg.get("p2p_token")
    generated = False
    if not token:
        token = secrets.token_urlsafe(24)
        cfg["p2p_token"] = token
        save_config(cfg)
        generated = True
    payload = {
        "v": PAIRING_VERSION,
        "token": token,
        "name": cfg.get("cortana_name", "Cortana"),
        "node_id": get_node_id(),
    }
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return (PAIRING_PREFIX + raw, {
        "token_fingerprint": _token_fingerprint(token),
        "endpoint": _local_endpoint(port),
        "node_id": payload["node_id"],
        "name": payload["name"],
        "token_generated": generated,
    })


def parse_pairing_code(code):
    """Decode a pairing code back to its payload dict. Raises ValueError."""
    code = (code or "").strip()
    if not code.startswith(PAIRING_PREFIX):
        raise ValueError("not a Cortana pairing code (expected to start with "
                         f"'{PAIRING_PREFIX}')")
    raw = code[len(PAIRING_PREFIX):]
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
    except Exception as e:
        raise ValueError(f"pairing code is corrupted: {e}")
    if payload.get("v") != PAIRING_VERSION or not payload.get("token") \
            or not payload.get("node_id"):
        raise ValueError("pairing code is missing required fields")
    return payload


def render_pairing_qr(code):
    """ASCII QR of the pairing payload, or None when `qrcode` is absent."""
    try:
        import qrcode
    except ImportError:
        return None
    try:
        import io
        qr = qrcode.QRCode(border=1)
        qr.add_data(code)
        qr.make()
        buf = io.StringIO()
        if hasattr(qr, "print_ascii"):
            qr.print_ascii(out=buf)
        else:  # very old qrcode versions
            return None
        return buf.getvalue()
    except Exception:
        return None


def _confirm_overwrite_token():
    from core.config import load_config
    if load_config().get("p2p_token"):
        from core.approvals import confirm
        return confirm("Replace your existing P2P token with the peer's?",
                       sensitive=True)
    return True


def join_pairing(code, port=11435, timeout=2.0, discovery_port=11436,
                 use_tls=None, verify_tls=False, assume_yes=False):
    """Join using a pairing code. Returns a result dict.

    On the LAN: discovers peers via UDP broadcast, matches the pairing
    payload's node_id against live status probes, stores the shared token,
    and verifies with an authenticated call. Off-LAN: returns ok=False
    with explicit port-forward instructions (no fake cloud pairing).
    """
    from core.config import load_config, save_config
    from core import p2p as p2p_mod

    try:
        payload = parse_pairing_code(code)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    want_node = str(payload["node_id"])
    peers = p2p_mod.p2p_peers(port=port, timeout=timeout, use_tls=use_tls,
                             verify_tls=verify_tls,
                             discovery_port=discovery_port)
    match = next((p for p in peers if p["node_id"] == want_node
                  and "lan" in p["sources"]), None)
    if match is None:
        return {
            "ok": False,
            "error": f"peer '{payload.get('name')}' (node {want_node[:8]}…) "
                     "not found on the local network",
            "hint": (
                "Pairing codes cannot cross NAT by themselves. To pair over "
                "the internet:\n"
                "  1. On the peer's network, port-forward TCP 11435 to the "
                "peer machine.\n"
                "  2. On the peer, set \"p2p_public_endpoint\": "
                "\"<peer-host>:11435\" in ~/.cortana/config.json\n"
                "     (alternatively run a discovery server with "
                "`cortana p2p-discovery` and set \"discovery_server_url\" "
                "on both nodes).\n"
                "  3. Re-run: cortana p2p-join <code>"
            ),
        }

    if not assume_yes and not _confirm_overwrite_token():
        return {"ok": False, "error": "join cancelled (token not replaced)"}

    cfg = load_config()
    cfg["p2p_token"] = payload["token"]
    save_config(cfg)

    lan = match["lan_endpoint"]
    res = p2p_mod.send_remote_command(
        lan["ip"], "list_tokens", {}, port=lan["port"],
        use_tls=use_tls, verify_tls=verify_tls)
    if not res["ok"]:
        return {"ok": False,
                "error": f"token stored but peer did not accept it: {res['error']}"}
    return {"ok": True, "peer": match["name"], "ip": lan["ip"],
            "port": lan["port"],
            "token_fingerprint": _token_fingerprint(payload["token"])}
