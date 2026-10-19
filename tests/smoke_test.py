#!/usr/bin/env python3
"""End-to-end smoke test for h_proxy -- standard library only.

Starts real backend servers and real proxy listeners on ephemeral ports, then
drives traffic through them. Also parses the shipped example config to exercise
the mini-YAML parser. Run with:

    python tests/smoke_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from h_proxy import config as cfg
from h_proxy.config import Config, ListenerConfig
from h_proxy.http_proxy import HTTPProxy
from h_proxy.tcp_proxy import TCPProxy

PASSED = 0


def check(cond: bool, msg: str) -> None:
    global PASSED
    if not cond:
        raise AssertionError(msg)
    PASSED += 1
    print(f"  ok - {msg}")


# --------------------------------------------------------------------------- #
# Backend servers
# --------------------------------------------------------------------------- #

async def _read_chunked(reader) -> bytes:
    out = b""
    while True:
        line = await reader.readline()
        size = int(line.strip().split(b";")[0], 16)
        if size == 0:
            await reader.readline()  # trailing blank line
            break
        out += await reader.readexactly(size)
        await reader.readexactly(2)
    return out


async def backend_http(reader, writer):
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    proxy_line = ""
    if lines and lines[0].startswith(b"PROXY"):  # strip optional PROXY protocol v1 line
        proxy_line = lines[0].decode()
        lines = lines[1:]
    target = (lines[0].decode().split(" ") + ["", "", ""])[1]
    headers = {}
    for line in lines[1:]:
        if b":" in line:
            k, _, v = line.partition(b":")
            headers[k.decode().strip().lower()] = v.decode().strip()

    body = b""
    if "content-length" in headers:
        n = int(headers["content-length"])
        body = await reader.readexactly(n) if n else b""
    elif "chunked" in headers.get("transfer-encoding", "").lower():
        body = await _read_chunked(reader)

    if target == "/chunked":
        writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        for part in (b"hello ", b"world"):
            writer.write(b"%x\r\n%s\r\n" % (len(part), part))
        writer.write(b"0\r\n\r\n")
    elif target == "/close":
        # No Content-Length: body is delimited by connection close.
        writer.write(b"HTTP/1.1 200 OK\r\n\r\nclosebody")
    else:
        payload = (
            f"xff={headers.get('x-forwarded-for', '')};"
            f"xrip={headers.get('x-real-ip', '')};"
            f"proto={headers.get('x-forwarded-proto', '')};"
            f"pp={proxy_line};"
            f"body={body.decode(errors='replace')}"
        ).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(payload), payload))

    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def backend_echo(reader, writer):
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


# --------------------------------------------------------------------------- #
# Client helpers
# --------------------------------------------------------------------------- #

async def read_response(reader):
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])
    headers = {}
    for line in lines[1:]:
        if line == b"":
            break
        if b":" in line:
            k, _, v = line.partition(b":")
            headers[k.decode().strip().lower()] = v.decode().strip()
    if "content-length" in headers:
        n = int(headers["content-length"])
        body = await reader.readexactly(n) if n else b""
    elif "chunked" in headers.get("transfer-encoding", "").lower():
        body = await _read_chunked(reader)
    else:
        body = await reader.read()
    return status, headers, body


async def port_of(server) -> int:
    return server.sockets[0].getsockname()[1]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

async def run() -> None:
    config = Config(connect_timeout=5.0)

    # Backends --------------------------------------------------------------
    http_be = await asyncio.start_server(backend_http, "127.0.0.1", 0)
    echo_be = await asyncio.start_server(backend_echo, "127.0.0.1", 0)
    http_be_port = await port_of(http_be)
    echo_be_port = await port_of(echo_be)

    # Proxies ---------------------------------------------------------------
    http_lc = ListenerConfig("web", "http", "127.0.0.1", 0, "127.0.0.1", http_be_port)
    tcp_lc = ListenerConfig("ssh", "tcp", "127.0.0.1", 0, "127.0.0.1", echo_be_port)
    pp_lc = ListenerConfig("pp", "http", "127.0.0.1", 0, "127.0.0.1", http_be_port, send_proxy="v1")
    src_lc = ListenerConfig("src", "tcp", "127.0.0.1", 0, "127.0.0.1", echo_be_port, source="127.0.0.1")
    http_px = await asyncio.start_server(
        HTTPProxy(http_lc, config).handle, "127.0.0.1", 0, limit=config.max_header_bytes
    )
    tcp_px = await asyncio.start_server(TCPProxy(tcp_lc, config).handle, "127.0.0.1", 0)
    pp_px = await asyncio.start_server(
        HTTPProxy(pp_lc, config).handle, "127.0.0.1", 0, limit=config.max_header_bytes
    )
    src_px = await asyncio.start_server(TCPProxy(src_lc, config).handle, "127.0.0.1", 0)
    http_port = await port_of(http_px)
    tcp_port = await port_of(tcp_px)
    pp_port = await port_of(pp_px)
    src_port = await port_of(src_px)

    try:
        # 1. GET with forwarded headers -----------------------------------
        r, w = await asyncio.open_connection("127.0.0.1", http_port)
        w.write(b"GET /hi HTTP/1.1\r\nHost: example.com\r\nConnection: close\r\n\r\n")
        await w.drain()
        status, _, body = await read_response(r)
        w.close()
        check(status == 200, "GET returns 200")
        check(b"xff=127.0.0.1" in body, "backend sees X-Forwarded-For = client ip")
        check(b"xrip=127.0.0.1" in body, "backend sees X-Real-IP = client ip")
        check(b"proto=http" in body, "backend sees X-Forwarded-Proto = http")

        # 2. POST with a body ---------------------------------------------
        r, w = await asyncio.open_connection("127.0.0.1", http_port)
        payload = b"the-body"
        w.write(
            b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
            % (len(payload), payload)
        )
        await w.drain()
        status, _, body = await read_response(r)
        w.close()
        check(status == 200 and b"body=the-body" in body, "POST body reaches backend intact")

        # 3. Chunked response passes through ------------------------------
        r, w = await asyncio.open_connection("127.0.0.1", http_port)
        w.write(b"GET /chunked HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        await w.drain()
        status, _, body = await read_response(r)
        w.close()
        check(status == 200 and body == b"hello world", "chunked response reassembles correctly")

        # 4. Response delimited by close ----------------------------------
        r, w = await asyncio.open_connection("127.0.0.1", http_port)
        w.write(b"GET /close HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        await w.drain()
        status, _, body = await read_response(r)
        w.close()
        check(status == 200 and body == b"closebody", "close-delimited response forwarded")

        # 5. Keep-alive: two requests on one connection -------------------
        r, w = await asyncio.open_connection("127.0.0.1", http_port)
        w.write(b"GET /a HTTP/1.1\r\nHost: x\r\n\r\n")
        await w.drain()
        s1, _, _ = await read_response(r)
        w.write(b"GET /b HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        await w.drain()
        s2, _, _ = await read_response(r)
        w.close()
        check(s1 == 200 and s2 == 200, "two requests served on one keep-alive connection")

        # 6. Raw TCP echo through the tcp proxy ---------------------------
        r, w = await asyncio.open_connection("127.0.0.1", tcp_port)
        w.write(b"ping-through-tcp")
        await w.drain()
        echoed = await r.readexactly(len(b"ping-through-tcp"))
        w.close()
        check(echoed == b"ping-through-tcp", "raw TCP bytes forwarded both ways")

        # 7. PROXY protocol v1 reaches the backend ------------------------
        r, w = await asyncio.open_connection("127.0.0.1", pp_port)
        w.write(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        await w.drain()
        status, _, body = await read_response(r)
        w.close()
        check(status == 200, "PROXY-protocol listener still serves 200")
        check(b"pp=PROXY TCP4 127.0.0.1 127.0.0.1" in body, "backend receives PROXY v1 header")

        # 8. Source-IP bind does not break forwarding ---------------------
        r, w = await asyncio.open_connection("127.0.0.1", src_port)
        w.write(b"src-bind-echo")
        await w.drain()
        echoed = await r.readexactly(len(b"src-bind-echo"))
        w.close()
        check(echoed == b"src-bind-echo", "source-bound TCP connection forwards")
    finally:
        for s in (http_px, tcp_px, pp_px, src_px, http_be, echo_be):
            s.close()
            await s.wait_closed()


def test_config_parser() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    example = os.path.join(os.path.dirname(here), "config.example.yaml")
    c = cfg.load(example)
    check(len(c.listeners) == 3, "example config parses 3 listeners")
    web = next(l for l in c.listeners if l.name == "web")
    check(web.mode == "http" and web.listen_port == 8080, "web listener parsed")
    check(web.backend_host == "127.0.0.1" and web.backend_port == 8000, "web backend parsed")
    check(web.forwarded_headers is True, "boolean value parsed")

    # A few parser edge cases.
    doc = cfg.parse_yaml(
        "a: 1\n"
        "b: true\n"
        'c: "quoted: value"\n'
        "d: bare text  # trailing comment\n"
        "items:\n"
        "  - one\n"
        "  - two\n"
    )
    check(doc["a"] == 1 and doc["b"] is True, "int and bool scalars")
    check(doc["c"] == "quoted: value", "quoted string keeps its colon")
    check(doc["d"] == "bare text", "trailing comment stripped from bare scalar")
    check(doc["items"] == ["one", "two"], "block list of scalars")

    # New options: send_proxy, source, max_connections.
    c2 = cfg.loads(
        "max_connections: 100\n"
        "listeners:\n"
        "  - name: a\n"
        "    mode: tcp\n"
        '    listen: ":22"\n'
        '    backend: "h:22"\n'
        "    send_proxy: v2\n"
        "    source: 10.0.0.5\n"
    )
    check(c2.max_connections == 100, "max_connections parsed")
    check(c2.listeners[0].send_proxy == "v2", "send_proxy parsed")
    check(c2.listeners[0].source == "10.0.0.5", "source parsed")


def test_proxyproto() -> None:
    import struct

    from h_proxy import proxyproto

    v1 = proxyproto.build_v1(("1.2.3.4", 5555), ("6.7.8.9", 80))
    check(v1 == b"PROXY TCP4 1.2.3.4 6.7.8.9 5555 80\r\n", "PROXY v1 line formatted")
    check(
        proxyproto.build_v1(("bad", 0), ("x", 0)) == b"PROXY UNKNOWN\r\n",
        "PROXY v1 falls back to UNKNOWN on bad address",
    )

    v2 = proxyproto.build_v2(("1.2.3.4", 5555), ("6.7.8.9", 80))
    check(v2.startswith(b"\r\n\r\n\x00\r\nQUIT\n"), "PROXY v2 signature present")
    check(v2[12] == 0x21 and v2[13] == 0x11, "PROXY v2 version/command and TCP4 family bytes")
    length = struct.unpack("!H", v2[14:16])[0]
    check(length == 12 and len(v2) == 16 + 12, "PROXY v2 IPv4 address block length correct")


def main() -> int:
    print("config parser:")
    test_config_parser()
    print("proxy protocol headers:")
    test_proxyproto()
    print("proxy traffic:")
    asyncio.run(run())
    print(f"\nAll {PASSED} checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        sys.exit(1)
