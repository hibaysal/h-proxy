# h_proxy

A small **pure-standard-library** TCP / HTTP / SOCKS5 proxy for Python 3.11+ —
think of it as a tiny "haproxy-lite". The `tcp` and `http` modes are reverse
proxies (each listener forwards to one fixed backend, which sees the proxy's own
IP as the source — the "SNAT" behaviour); the `socks` mode is a SOCKS5 forward
proxy where the client picks the destination per connection.

No third-party dependencies. No `pip install`. Just `asyncio` from the standard
library. Runs on Linux, macOS, and Windows.

## Features

- **TCP mode** — raw byte forwarding for SSH or any opaque protocol.
- **HTTP mode** — HTTP/1.x-aware forwarding that injects the usual reverse-proxy
  headers: `X-Forwarded-For`, `X-Real-IP`, `X-Forwarded-Proto`, `X-Forwarded-Host`.
  Handles keep-alive, `Content-Length` and chunked bodies, `Expect: 100-continue`,
  and `Connection: Upgrade` (WebSocket) by splicing the connection raw after `101`.
- **SOCKS5 mode** — a forward proxy (RFC 1928, CONNECT) where the client chooses
  the destination: works for browsing *and* SSH and anything else TCP. Optional
  username/password auth (RFC 1929); hostnames are resolved proxy-side (remote
  DNS). No `backend` — the destination is dynamic.
- **PROXY protocol** (`send_proxy: v1|v2`) and outbound **source-IP bind**
  (`source:`) to preserve / steer the client address.
- **YAML config** — parsed by a tiny built-in parser (a documented subset), so
  there's nothing to install.
- Multiple listeners in one process; clean shutdown on Ctrl+C / `SIGTERM`.

## Quick start

```bash
# 1. Write a config (start from the example)
cp config.example.yaml config.yaml

# 2. Run it
python -m h_proxy config.yaml
# (config.yaml is the default, so `python -m h_proxy` also works)
```

Useful flags:

```bash
python -m h_proxy --log-level DEBUG config.yaml
python -m h_proxy --version
```

## Configuration

```yaml
# Optional global tuning (defaults shown):
connect_timeout: 10          # seconds to wait when dialling a backend
buffer_size: 65536           # bytes per read in raw forwarding
# idle_timeout: 300          # drop a connection after N seconds of silence
# max_header_bytes: 1048576  # cap on an HTTP request/response head
# max_connections: 1000      # soft cap on concurrent connections (default: unlimited)

listeners:
  - name: web                # unique label, used in logs
    mode: http               # http | tcp
    listen: "0.0.0.0:8080"   # ":8080" binds all interfaces; [::1]:8080 for IPv6
    backend: "127.0.0.1:8000"  # any IP or hostname, local or remote
    forwarded_headers: true  # http only: inject X-Forwarded-* (default true)
    # host: example.com      # http only: only serve this Host, else 404
    # send_proxy: v2         # prepend PROXY protocol header to the backend
    # source: 10.0.0.1       # bind outbound connections to this local IP

  - name: ssh
    mode: tcp
    listen: "0.0.0.0:2222"
    backend: "127.0.0.1:22"
```

### Supported YAML subset

The built-in parser intentionally supports only what the config needs:

- `key: value` mappings and block lists (`- item`).
- Nesting by **space** indentation (tabs are rejected).
- `#` comments, whole-line or trailing an unquoted/quoted value.
- Scalars: bare words (parsed as bool / int / float / null / string) and single-
  or double-quoted strings.

Flow style (`[a, b]`, `{a: b}`), anchors, multi-document streams and block
scalars (`|`, `>`) are **not** supported. The example config stays within the
subset. (If you'd rather use real YAML, swap `h_proxy/config.py`'s parser for
`import yaml` — but that reintroduces a dependency.)

## How "SNAT" works here

When the proxy forwards a connection it opens an ordinary outbound socket to the
backend, so the backend sees the **proxy's** address as the source. In HTTP mode
the original client IP is preserved for the application via `X-Forwarded-For` /
`X-Real-IP`.

### Preserving the real client IP at L4 (PROXY protocol)

For `tcp` mode (or HTTPS you don't want to decrypt), set `send_proxy` so the
proxy prepends a HAProxy **PROXY protocol** header to each backend connection.
The backend learns the real client address without any root/TPROXY setup — it
just has to be told to expect it (e.g. nginx `proxy_protocol`, a haproxy
`accept-proxy` backend):

```yaml
  - name: https
    mode: tcp
    listen: "0.0.0.0:443"
    backend: "10.0.0.10:443"
    send_proxy: v2        # false (default) | v1 (text) | v2 (binary)
```

### Binding the outbound source IP (`source`)

If the proxy host has several addresses, `source` picks which local IP the
backend connections originate from (equivalent to haproxy's `source` directive):

```yaml
    source: 10.0.0.1
```

Transparent proxying that spoofs the client IP at L4 (so the backend sees the
client directly) would need root + Linux TPROXY and is out of scope; the PROXY
protocol is the portable alternative.

## Example: browse and SSH through one SSH reverse tunnel

A SOCKS5 listener plus a single `ssh -R` reverse tunnel lets a remote machine
reach *everything* on the proxy host's network — web and SSH alike — without a
port-per-host.

```
  home browser / ssh ──socks5h──▶ homeserver:6969
                                       │  ssh -R 6969:localhost:1080 homeserver
                                       ▼            (run from the laptop)
  laptop (runs h_proxy)  ── SOCKS5 127.0.0.1:1080 ──▶ VPN ──▶ data center / internet
```

On the **laptop** (which has the VPN), run h_proxy with just a SOCKS listener
bound to localhost, then open the reverse tunnel:

```yaml
listeners:
  - name: socks
    mode: socks
    listen: "127.0.0.1:1080"   # only reachable via the SSH tunnel -> SSH is the auth
    # source: 10.8.0.2         # optional: force egress out the VPN interface
```
```bash
ssh -R 6969:localhost:1080 homeserver   # expose the SOCKS proxy at homeserver:6969
```

At **home**:

- **Firefox** → SOCKS v5 host `127.0.0.1` port `6969`, and tick *"Proxy DNS when
  using SOCKS v5"* so names resolve inside the VPN.
- **SSH** (no per-host tunnels needed) in `~/.ssh/config`:
  ```sshconfig
  Host dc-*
      ProxyCommand ncat --proxy-type socks5 --proxy 127.0.0.1:6969 --proxy-dns remote %h %p
      # or: ProxyCommand nc -X 5 -x 127.0.0.1:6969 %h %p
  ```
  Then `ssh dc-db01` tunnels straight through.

Keep the tunnel resilient (auto-reconnect, and fail fast if the remote port is
still held by a stale session):

```bash
autossh -M 0 -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes -R 6969:localhost:1080 homeserver
```

By default `-R` binds `6969` to **loopback** on the home server, so only the home
server itself can use it. To let other devices on the home LAN reach it, set
`GatewayPorts clientspecified` in the home server's `sshd_config` and bind
`-R 0.0.0.0:6969:localhost:1080` — and then set `socks_auth`, because the DC exit
is now reachable by your whole LAN.

Ready-to-copy versions of both halves live in [`examples/`](examples/):
[`examples/ssh_config`](examples/ssh_config) for the home machine and
[`examples/laptop-tunnel.sh`](examples/laptop-tunnel.sh) for the laptop.

Because SOCKS can reach anything the laptop can, keep the listener on `127.0.0.1`
(so only the authenticated SSH tunnel can use it), or set `socks_auth`, or use
fixed `tcp` listeners instead when you want to restrict the reachable hosts.

## Testing

```bash
python tests/smoke_test.py
```

This starts real backends and proxy listeners on ephemeral ports and pushes HTTP
and raw-TCP traffic through them, and also parses the example config to exercise
the YAML parser.

## Layout

```
h_proxy/
  config.py      # mini-YAML parser + config dataclasses + validation
  relay.py       # bidirectional raw byte pump (half-close aware)
  tcp_proxy.py   # raw TCP listener (SSH + anything)
  http_proxy.py  # HTTP/1.x-aware listener
  socks_proxy.py # SOCKS5 forward-proxy listener
  proxyproto.py  # HAProxy PROXY protocol v1/v2 header builder
  server.py      # starts listeners, graceful shutdown
  __main__.py    # CLI entry point
config.example.yaml
examples/
  ssh_config        # home-side ~/.ssh/config for SSH-over-SOCKS
  laptop-tunnel.sh  # laptop-side autossh/ssh reverse-tunnel launcher
tests/smoke_test.py
```

## Limitations

- `tcp`/`http`: one backend per listener (no load balancing / health checks).
- HTTP mode is a pragmatic HTTP/1.x forwarder, not a conformance-complete
  implementation; HTTP/2 is not supported (use `tcp` mode to pass TLS/h2 through
  opaquely).
- Hop-by-hop header handling is minimal (the `Connection` header is managed; a
  full hop-by-hop strip is not performed).
- SOCKS: SOCKS5 only, `CONNECT` only (no SOCKS4/4a, no `BIND`, no UDP
  `ASSOCIATE`), and no per-destination allowlist yet.
