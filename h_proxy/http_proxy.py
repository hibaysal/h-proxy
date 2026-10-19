"""HTTP/1.x-aware proxy listener.

Forwards requests to a single backend while injecting the usual reverse-proxy
headers (``X-Forwarded-For`` / ``X-Real-IP`` / ``X-Forwarded-Proto`` /
``X-Forwarded-Host``). It understands just enough of HTTP/1.x to be correct:

* keep-alive connections (multiple requests per client connection),
* ``Content-Length`` and ``Transfer-Encoding: chunked`` bodies, both ways,
* ``Expect: 100-continue`` and other 1xx interim responses,
* ``Connection: Upgrade`` (e.g. WebSocket) -- after a ``101`` the connection is
  spliced raw in both directions.

It is not a conformance-complete HTTP implementation; it is a pragmatic
forwarder. One backend connection is opened per client request.
"""

from __future__ import annotations

import asyncio
import logging

from . import proxyproto
from .config import Config, ListenerConfig
from .relay import splice

log = logging.getLogger("h_proxy.http")

_REASONS = {
    400: "Bad Request",
    404: "Not Found",
    431: "Request Header Fields Too Large",
    502: "Bad Gateway",
}


# --------------------------------------------------------------------------- #
# Header parsing / building (HTTP header octets are latin-1)
# --------------------------------------------------------------------------- #

def _parse_head(block: bytes) -> tuple[list[str], list[list[str]]]:
    """Split a ``...\\r\\n\\r\\n`` head into start-line parts and headers."""
    lines = block.split(b"\r\n")
    start = lines[0].decode("latin-1").split(" ", 2)
    headers: list[list[str]] = []
    for raw in lines[1:]:
        if raw == b"":
            break
        if raw[:1] in (b" ", b"\t"):  # obsolete line folding
            if headers:
                headers[-1][1] += " " + raw.decode("latin-1").strip()
            continue
        name, sep, value = raw.partition(b":")
        if not sep:
            raise ValueError("header line without colon")
        headers.append([name.decode("latin-1").strip(), value.decode("latin-1").strip()])
    return start, headers


def _build_head(start_parts: list[str], headers: list[list[str]]) -> bytes:
    out = [" ".join(start_parts)]
    out += [f"{name}: {value}" for name, value in headers]
    out += ["", ""]
    return "\r\n".join(out).encode("latin-1")


def _get(headers: list[list[str]], name: str, default: str | None = None) -> str | None:
    low = name.lower()
    for n, v in headers:
        if n.lower() == low:
            return v
    return default


def _set(headers: list[list[str]], name: str, value: str) -> None:
    """Set a header, replacing the first match and dropping any duplicates."""
    low = name.lower()
    done = False
    kept: list[list[str]] = []
    for pair in headers:
        if pair[0].lower() == low:
            if not done:
                pair[1] = value
                kept.append(pair)
                done = True
        else:
            kept.append(pair)
    if not done:
        kept.append([name, value])
    headers[:] = kept


def _remove(headers: list[list[str]], name: str) -> None:
    low = name.lower()
    headers[:] = [p for p in headers if p[0].lower() != low]


def _apply_forwarded(headers: list[list[str]], client_ip: str) -> None:
    existing = _get(headers, "x-forwarded-for")
    if existing:
        _set(headers, "X-Forwarded-For", f"{existing}, {client_ip}")
    else:
        headers.append(["X-Forwarded-For", client_ip])
    # Overwrite client-supplied trust headers so they cannot be spoofed.
    _set(headers, "X-Real-IP", client_ip)
    _set(headers, "X-Forwarded-Proto", "http")
    host = _get(headers, "host")
    if host and _get(headers, "x-forwarded-host") is None:
        headers.append(["X-Forwarded-Host", host])


def _client_keepalive(version: str, headers: list[list[str]]) -> bool:
    tokens = [t.strip().lower() for t in (_get(headers, "connection", "") or "").split(",")]
    if version == "HTTP/1.0":
        return "keep-alive" in tokens
    return "close" not in tokens


async def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except (OSError, ConnectionError):
        pass


# --------------------------------------------------------------------------- #
# Proxy
# --------------------------------------------------------------------------- #

class HTTPProxy:
    def __init__(self, listener: ListenerConfig, config: Config) -> None:
        self.listener = listener
        self.config = config

    # -- body copiers ------------------------------------------------------- #

    async def _copy_exact(self, src, dst, n: int) -> None:
        remaining = n
        bufsize = self.config.buffer_size
        while remaining > 0:
            chunk = await src.read(min(bufsize, remaining))
            if not chunk:
                raise ConnectionError("connection closed mid-body")
            dst.write(chunk)
            await dst.drain()
            remaining -= len(chunk)

    async def _copy_until_eof(self, src, dst) -> None:
        bufsize = self.config.buffer_size
        while True:
            chunk = await src.read(bufsize)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()

    async def _copy_chunked(self, src, dst) -> None:
        bufsize = self.config.buffer_size
        while True:
            size_line = await src.readline()
            if not size_line:
                raise ConnectionError("connection closed in chunked body")
            dst.write(size_line)
            token = size_line.split(b";", 1)[0].strip()
            try:
                size = int(token, 16)
            except ValueError:
                raise ConnectionError(f"bad chunk size: {token!r}") from None
            if size == 0:  # last chunk; copy optional trailers then the blank line
                await dst.drain()
                while True:
                    line = await src.readline()
                    dst.write(line)
                    if line in (b"\r\n", b"\n", b""):
                        break
                await dst.drain()
                return
            remaining = size
            while remaining > 0:
                chunk = await src.read(min(bufsize, remaining))
                if not chunk:
                    raise ConnectionError("connection closed in chunk")
                dst.write(chunk)
                remaining -= len(chunk)
            dst.write(await src.readexactly(2))  # trailing CRLF
            await dst.drain()

    async def _copy_body(self, src, dst, headers, *, is_request, method=None, status=None) -> None:
        if not is_request and (
            method == "HEAD"
            or (status is not None and (status in (204, 304) or 100 <= status < 200))
        ):
            return
        te = _get(headers, "transfer-encoding", "") or ""
        if "chunked" in te.lower():
            await self._copy_chunked(src, dst)
            return
        cl = _get(headers, "content-length")
        if cl is not None:
            try:
                n = int(cl.strip())
            except ValueError:
                raise ConnectionError(f"bad content-length: {cl!r}") from None
            await self._copy_exact(src, dst, n)
            return
        if is_request:
            return  # no framing on a request -> assume no body
        await self._copy_until_eof(src, dst)

    # -- helpers ------------------------------------------------------------ #

    async def _simple(self, writer, code: int) -> None:
        reason = _REASONS.get(code, "Error")
        body = f"{code} {reason}\r\n".encode()
        head = (
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("latin-1")
        try:
            writer.write(head + body)
            await writer.drain()
        except (ConnectionError, OSError):
            pass

    @staticmethod
    async def _await_quiet(task: asyncio.Task) -> None:
        try:
            await task
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 - best effort
            pass

    async def _read_final_response(self, breader, cwriter):
        """Read response heads, forwarding 1xx interim ones, until the final."""
        while True:
            try:
                head = await breader.readuntil(b"\r\n\r\n")
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
                return None, None, None
            start, headers = _parse_head(head)
            if len(start) < 2:
                return None, None, None
            try:
                status = int(start[1])
            except ValueError:
                return None, None, None
            if 100 <= status < 200 and status != 101:
                cwriter.write(_build_head(start, headers))  # interim, pass through
                await cwriter.drain()
                continue
            return status, start, headers

    # -- connection handler ------------------------------------------------- #

    async def handle(self, creader: asyncio.StreamReader, cwriter: asyncio.StreamWriter) -> None:
        peer = cwriter.get_extra_info("peername")
        sock = cwriter.get_extra_info("sockname")
        client_ip = peer[0] if peer else "?"
        peer_repr = f"{peer[0]}:{peer[1]}" if peer else "?"
        lc = self.listener
        keep_alive = True

        try:
            while keep_alive:
                try:
                    head = await creader.readuntil(b"\r\n\r\n")
                except asyncio.IncompleteReadError:
                    break  # client closed, possibly between keep-alive requests
                except asyncio.LimitOverrunError:
                    await self._simple(cwriter, 431)
                    break
                except (ConnectionError, asyncio.TimeoutError):
                    break

                try:
                    req_line, req_headers = _parse_head(head)
                except ValueError:
                    await self._simple(cwriter, 400)
                    break
                if len(req_line) < 3:
                    await self._simple(cwriter, 400)
                    break

                method, _, version = req_line[0].upper(), req_line[1], req_line[2]
                keep_alive = _client_keepalive(version, req_headers)

                if lc.host is not None:
                    host_value = _get(req_headers, "host", "") or ""
                    if host_value.split(":")[0].lower() != lc.host.lower():
                        await self._simple(cwriter, 404)
                        break

                is_upgrade = (
                    "upgrade" in (_get(req_headers, "connection", "") or "").lower()
                    and _get(req_headers, "upgrade") is not None
                )

                if lc.forwarded_headers:
                    _apply_forwarded(req_headers, client_ip)

                if not is_upgrade:
                    # one backend connection per request: ask the backend to close.
                    _remove(req_headers, "connection")
                    _remove(req_headers, "proxy-connection")
                    _remove(req_headers, "keep-alive")
                    _set(req_headers, "Connection", "close")

                connect_kwargs = {"limit": self.config.max_header_bytes}
                if lc.source:
                    connect_kwargs["local_addr"] = (lc.source, 0)
                try:
                    breader, bwriter = await asyncio.wait_for(
                        asyncio.open_connection(lc.backend_host, lc.backend_port, **connect_kwargs),
                        timeout=self.config.connect_timeout,
                    )
                except (OSError, asyncio.TimeoutError) as exc:
                    log.warning("[%s] %s backend connect failed: %s", lc.name, peer_repr, exc)
                    await self._simple(cwriter, 502)
                    break

                log.info("[%s] %s %s %s -> %s", lc.name, peer_repr, method, req_line[1], lc.backend_addr)
                try:
                    if lc.send_proxy and peer and sock:
                        bwriter.write(proxyproto.build(lc.send_proxy, peer, sock))
                    bwriter.write(_build_head(req_line, req_headers))
                    await bwriter.drain()

                    # Forward the request body concurrently with reading the
                    # response. This is the natural full-duplex model and makes
                    # Expect: 100-continue and upgrades work without deadlock.
                    body_task = asyncio.create_task(
                        self._copy_body(creader, bwriter, req_headers, is_request=True, method=method)
                    )

                    status, status_line, resp_headers = await self._read_final_response(breader, cwriter)
                    if status is None:
                        body_task.cancel()
                        await self._await_quiet(body_task)
                        await self._simple(cwriter, 502)
                        break

                    if status == 101 and is_upgrade:
                        cwriter.write(_build_head(status_line, resp_headers))
                        await cwriter.drain()
                        body_task.cancel()
                        await self._await_quiet(body_task)
                        await splice(
                            creader, cwriter, breader, bwriter,
                            self.config.buffer_size, self.config.idle_timeout,
                        )
                        return

                    resp_bodyless = (
                        method == "HEAD" or status in (204, 304) or 100 <= status < 200
                    )
                    resp_te = (_get(resp_headers, "transfer-encoding", "") or "").lower()
                    resp_cl = _get(resp_headers, "content-length")
                    if not resp_bodyless and "chunked" not in resp_te and resp_cl is None:
                        keep_alive = False  # response delimited by close

                    # Decide the client-facing Connection header ourselves.
                    _remove(resp_headers, "connection")
                    _remove(resp_headers, "keep-alive")
                    if keep_alive:
                        if version == "HTTP/1.0":
                            _set(resp_headers, "Connection", "keep-alive")
                        # HTTP/1.1 keep-alive is the default; no header needed
                    else:
                        _set(resp_headers, "Connection", "close")

                    cwriter.write(_build_head(status_line, resp_headers))
                    await cwriter.drain()
                    await self._copy_body(
                        breader, cwriter, resp_headers,
                        is_request=False, method=method, status=status,
                    )

                    try:
                        await body_task
                    except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
                        keep_alive = False
                except (ConnectionError, asyncio.IncompleteReadError):
                    keep_alive = False
                finally:
                    await _close(bwriter)
        finally:
            await _close(cwriter)
