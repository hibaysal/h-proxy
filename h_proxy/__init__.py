"""h_proxy: a small, pure-standard-library TCP/HTTP proxy.

A deliberately simple "haproxy-lite": each listener accepts connections and
forwards them to a single backend. Two modes are supported:

* ``tcp``  -- raw byte forwarding, used for SSH and any opaque protocol.
* ``http`` -- HTTP/1.x-aware forwarding that injects X-Forwarded-* headers.

The backend always sees the proxy's own source address (ordinary outbound
socket), which is the "SNAT" behaviour wanted here.

No third-party dependencies: only the Python standard library is used.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
