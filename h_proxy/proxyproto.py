"""HAProxy PROXY protocol header generation (v1 and v2).

When ``send_proxy`` is enabled on a listener, the proxy prepends one of these
headers to the *backend* connection before any other bytes. A backend that
understands the PROXY protocol (nginx ``proxy_protocol``, haproxy
``accept-proxy``, etc.) then learns the real client address even though the
transport-level source address is the proxy's own.

This is the standard way to preserve the client IP through a TCP proxy without
requiring root / Linux TPROXY. Spec: https://www.haproxy.org/download/2.8/doc/proxy-protocol.txt
"""

from __future__ import annotations

import ipaddress
import struct

# 12-byte signature that begins every v2 header.
_V2_SIG = b"\r\n\r\n\x00\r\nQUIT\n"


def _addr_pair(src, dst):
    """Return (ip_src, ip_dst) as ipaddress objects, or None if not usable."""
    try:
        sip = ipaddress.ip_address(src[0])
        dip = ipaddress.ip_address(dst[0])
    except (ValueError, IndexError, TypeError):
        return None
    if sip.version != dip.version:
        return None
    return sip, dip


def build_v1(src, dst) -> bytes:
    """Human-readable v1 line: ``PROXY TCP4 <src> <dst> <sport> <dport>\\r\\n``."""
    pair = _addr_pair(src, dst)
    if pair is None:
        return b"PROXY UNKNOWN\r\n"
    sip, dip = pair
    proto = "TCP4" if sip.version == 4 else "TCP6"
    return f"PROXY {proto} {sip} {dip} {src[1]} {dst[1]}\r\n".encode("ascii")


def build_v2(src, dst) -> bytes:
    """Binary v2 header."""
    pair = _addr_pair(src, dst)
    if pair is None:
        # LOCAL command / UNSPEC family: backend falls back to real addresses.
        return _V2_SIG + bytes([0x20, 0x00]) + struct.pack("!H", 0)
    sip, dip = pair
    ver_cmd = 0x21  # version 2 (high nibble), PROXY command (low nibble)
    if sip.version == 4:
        fam_proto = 0x11  # AF_INET + STREAM
    else:
        fam_proto = 0x21  # AF_INET6 + STREAM
    addr = sip.packed + dip.packed + struct.pack("!HH", src[1], dst[1])
    return _V2_SIG + bytes([ver_cmd, fam_proto]) + struct.pack("!H", len(addr)) + addr


def build(version: str, src, dst) -> bytes:
    """Dispatch on ``"v1"`` / ``"v2"``."""
    return build_v1(src, dst) if version == "v1" else build_v2(src, dst)
