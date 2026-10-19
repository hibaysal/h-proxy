# h_proxy

A small **pure-standard-library** TCP / HTTP proxy for Python 3.11+ — think of
it as a tiny "haproxy-lite" that just forwards connections to a backend. Each
listener accepts connections and forwards them to a single backend; the backend
sees the proxy's own IP as the source address (the "SNAT" behaviour).

No third-party dependencies. No `pip install`. Just `asyncio` from the standard
library. Runs on Linux, macOS, and Windows.

## Features

- **TCP mode** — raw byte forwarding for SSH or any opaque protocol.
- **HTTP mode** — HTTP/1.x-aware forwarding that injects the usual reverse-proxy
  headers: `X-Forwarded-For`, `X-Real-IP`, `X-Forwarded-Proto`, `X-Forwarded-Host`.
  Handles keep-alive, `Content-Length` and chunked bodies, `Expect: 100-continue`,
  and `Connection: Upgrade` (WebSocket) by splicing the connection raw after `101`.
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
  proxyproto.py  # HAProxy PROXY protocol v1/v2 header builder
  server.py      # starts listeners, graceful shutdown
  __main__.py    # CLI entry point
config.example.yaml
tests/smoke_test.py
```

## Limitations

- One backend per listener (no load balancing / health checks).
- HTTP mode is a pragmatic HTTP/1.x forwarder, not a conformance-complete
  implementation; HTTP/2 is not supported (use `tcp` mode to pass TLS/h2 through
  opaquely).
- Hop-by-hop header handling is minimal (the `Connection` header is managed; a
  full hop-by-hop strip is not performed).
