"""SOCKS5 forward proxy listener (RFC 1928 + RFC 1929 username/password).

Unlike the tcp/http reverse-proxy modes, a socks listener has no fixed backend:
the client negotiates and then names the destination per connection (CONNECT),
the proxy dials it, and the existing raw relay pumps bytes both ways.

Scope: SOCKS5, CONNECT command only. BIND and UDP ASSOCIATE are answered with
"command not supported". Authentication is optional -- if the listener is
configured with ``socks_auth`` (a username->password map) clients must present
valid RFC 1929 credentials; otherwise no-auth is accepted.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import socket
import struct

from .config import Config, ListenerConfig
from .relay import splice

log = logging.getLogger("h_proxy.socks")

# Authentication methods (RFC 1928 section 3).
_M_NO_AUTH = 0x00
_M_USERPASS = 0x02
_M_NONE_ACCEPTABLE = 0xFF

# Reply codes (RFC 1928 section 6).
_REP_OK = 0x00
_REP_GENERAL_FAILURE = 0x01
_REP_NET_UNREACHABLE = 0x03
_REP_HOST_UNREACHABLE = 0x04
_REP_CONN_REFUSED = 0x05
_REP_CMD_UNSUPPORTED = 0x07
_REP_ATYP_UNSUPPORTED = 0x08

# Address types.
_ATYP_IPV4 = 0x01
_ATYP_DOMAIN = 0x03
_ATYP_IPV6 = 0x04


async def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except (OSError, ConnectionError):
        pass


class SOCKS5Proxy:
    def __init__(self, listener: ListenerConfig, config: Config) -> None:
        self.listener = listener
        self.config = config
        self.auth = listener.socks_auth  # dict[str, str] | None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_repr = f"{peer[0]}:{peer[1]}" if peer else "?"
        lc = self.listener
        try:
            if not await self._negotiate(reader, writer, peer_repr):
                return
            target = await self._read_request(reader, writer, peer_repr)
            if target is None:
                return
            host, port = target
            await self._connect_and_relay(reader, writer, host, port, peer_repr)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            await _close(writer)

    # -- handshake ---------------------------------------------------------- #

    async def _negotiate(self, reader, writer, peer_repr) -> bool:
        ver, nmethods = await reader.readexactly(2)
        if ver != 0x05:
            return False  # not SOCKS5
        methods = set(await reader.readexactly(nmethods))

        if self.auth:
            if _M_USERPASS in methods:
                writer.write(bytes([0x05, _M_USERPASS]))
                await writer.drain()
                return await self._userpass(reader, writer, peer_repr)
            writer.write(bytes([0x05, _M_NONE_ACCEPTABLE]))
            await writer.drain()
            log.warning("[%s] %s offered no username/password method", self.listener.name, peer_repr)
            return False

        if _M_NO_AUTH in methods:
            writer.write(bytes([0x05, _M_NO_AUTH]))
            await writer.drain()
            return True
        writer.write(bytes([0x05, _M_NONE_ACCEPTABLE]))
        await writer.drain()
        return False

    async def _userpass(self, reader, writer, peer_repr) -> bool:
        ver = (await reader.readexactly(1))[0]
        if ver != 0x01:  # RFC 1929 sub-negotiation version
            return False
        ulen = (await reader.readexactly(1))[0]
        uname = await reader.readexactly(ulen)
        plen = (await reader.readexactly(1))[0]
        passwd = await reader.readexactly(plen)

        expected = self.auth.get(uname.decode("utf-8", "replace"))
        ok = expected is not None and hmac.compare_digest(passwd, expected.encode("utf-8"))
        writer.write(bytes([0x01, 0x00 if ok else 0x01]))
        await writer.drain()
        if not ok:
            log.warning("[%s] %s authentication failed", self.listener.name, peer_repr)
        return ok

    async def _read_request(self, reader, writer, peer_repr):
        ver, cmd, _rsv, atyp = await reader.readexactly(4)
        if ver != 0x05:
            await self._reply(writer, _REP_GENERAL_FAILURE)
            return None
        if cmd != 0x01:  # CONNECT only
            await self._reply(writer, _REP_CMD_UNSUPPORTED)
            log.warning("[%s] %s unsupported command 0x%02x", self.listener.name, peer_repr, cmd)
            return None

        if atyp == _ATYP_IPV4:
            host = socket.inet_ntoa(await reader.readexactly(4))
        elif atyp == _ATYP_IPV6:
            host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        elif atyp == _ATYP_DOMAIN:
            length = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(length)).decode("utf-8", "replace")
        else:
            await self._reply(writer, _REP_ATYP_UNSUPPORTED)
            return None

        port = struct.unpack("!H", await reader.readexactly(2))[0]
        return host, port

    # -- connect + relay ---------------------------------------------------- #

    async def _connect_and_relay(self, reader, writer, host, port, peer_repr) -> None:
        lc = self.listener
        connect_kwargs = {"limit": max(self.config.buffer_size, 1 << 16)}
        if lc.source:
            connect_kwargs["local_addr"] = (lc.source, 0)
        try:
            breader, bwriter = await asyncio.wait_for(
                asyncio.open_connection(host, port, **connect_kwargs),
                timeout=self.config.connect_timeout,
            )
        except asyncio.TimeoutError:
            await self._reply(writer, _REP_HOST_UNREACHABLE)
            return
        except ConnectionRefusedError:
            await self._reply(writer, _REP_CONN_REFUSED)
            return
        except socket.gaierror:
            await self._reply(writer, _REP_HOST_UNREACHABLE)
            return
        except OSError:
            await self._reply(writer, _REP_NET_UNREACHABLE)
            return

        await self._reply(writer, _REP_OK, bwriter.get_extra_info("sockname"))
        log.info("[%s] %s -> %s:%s open", lc.name, peer_repr, host, port)
        try:
            await splice(reader, writer, breader, bwriter, self.config.buffer_size, self.config.idle_timeout)
        finally:
            await _close(bwriter)
            log.info("[%s] %s -> %s:%s closed", lc.name, peer_repr, host, port)

    async def _reply(self, writer, rep, bnd=None) -> None:
        atyp, body = _ATYP_IPV4, b"\x00\x00\x00\x00\x00\x00"  # fallback 0.0.0.0:0
        if bnd:
            host, port = bnd[0], bnd[1]
            try:
                if ":" in host:
                    body = socket.inet_pton(socket.AF_INET6, host) + struct.pack("!H", port)
                    atyp = _ATYP_IPV6
                else:
                    body = socket.inet_aton(host) + struct.pack("!H", port)
                    atyp = _ATYP_IPV4
            except OSError:
                atyp, body = _ATYP_IPV4, b"\x00\x00\x00\x00\x00\x00"
        try:
            writer.write(bytes([0x05, rep, 0x00, atyp]) + body)
            await writer.drain()
        except (ConnectionError, OSError):
            pass
