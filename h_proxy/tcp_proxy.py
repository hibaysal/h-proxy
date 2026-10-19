"""Raw TCP proxy listener (used for SSH and any opaque protocol)."""

from __future__ import annotations

import asyncio
import logging

from . import proxyproto
from .config import Config, ListenerConfig
from .relay import splice

log = logging.getLogger("h_proxy.tcp")


async def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except (OSError, ConnectionError):
        pass


class TCPProxy:
    """Accept a connection and pump bytes to/from a single backend."""

    def __init__(self, listener: ListenerConfig, config: Config) -> None:
        self.listener = listener
        self.config = config

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        sock = writer.get_extra_info("sockname")
        peer_repr = f"{peer[0]}:{peer[1]}" if peer else "?"
        lc = self.listener

        connect_kwargs = {"limit": max(self.config.buffer_size, 1 << 16)}
        if lc.source:
            connect_kwargs["local_addr"] = (lc.source, 0)
        try:
            backend_reader, backend_writer = await asyncio.wait_for(
                asyncio.open_connection(lc.backend_host, lc.backend_port, **connect_kwargs),
                timeout=self.config.connect_timeout,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning(
                "[%s] %s -> %s connect failed: %s",
                lc.name, peer_repr, lc.backend_addr, exc,
            )
            await _close(writer)
            return

        log.info("[%s] %s <-> %s open", lc.name, peer_repr, lc.backend_addr)
        try:
            if lc.send_proxy and peer and sock:
                backend_writer.write(proxyproto.build(lc.send_proxy, peer, sock))
                await backend_writer.drain()
            await splice(
                reader,
                writer,
                backend_reader,
                backend_writer,
                self.config.buffer_size,
                self.config.idle_timeout,
            )
        finally:
            await _close(backend_writer)
            await _close(writer)
            log.info("[%s] %s <-> %s closed", lc.name, peer_repr, lc.backend_addr)
