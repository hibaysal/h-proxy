"""Wire listeners together and run them until shutdown.

Graceful shutdown uses POSIX signal handlers where available; on Windows
(which has no ``loop.add_signal_handler``) it falls back to ``KeyboardInterrupt``
(Ctrl+C), handled by the entry point.
"""

from __future__ import annotations

import asyncio
import logging

from .config import Config, ListenerConfig
from .http_proxy import HTTPProxy
from .socks_proxy import SOCKS5Proxy
from .tcp_proxy import TCPProxy

log = logging.getLogger("h_proxy.server")


def _make_proxy(listener: ListenerConfig, config: Config):
    if listener.mode == "http":
        return HTTPProxy(listener, config)
    if listener.mode == "socks":
        return SOCKS5Proxy(listener, config)
    return TCPProxy(listener, config)


async def serve(config: Config) -> None:
    """Start every listener and block until signalled to stop."""
    sem = asyncio.Semaphore(config.max_connections) if config.max_connections else None

    def wrap(handler):
        if sem is None:
            return handler

        async def limited(reader, writer):
            async with sem:
                await handler(reader, writer)

        return limited

    servers: list[asyncio.base_events.Server] = []
    for lc in config.listeners:
        proxy = _make_proxy(lc, config)
        limit = (
            config.max_header_bytes
            if lc.mode == "http"
            else max(config.buffer_size, 1 << 16)
        )
        server = await asyncio.start_server(
            wrap(proxy.handle), lc.listen_host, lc.listen_port, limit=limit
        )
        servers.append(server)
        dest = "dynamic (client-chosen)" if lc.mode == "socks" else lc.backend_addr
        log.info("listener %r (%s): %s -> %s", lc.name, lc.mode, lc.listen_addr, dest)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    have_signals = False
    try:
        import signal

        for signame in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, signame, None)
            if sig is not None:
                loop.add_signal_handler(sig, stop.set)
                have_signals = True
    except (NotImplementedError, RuntimeError):
        # Windows, or no running main-thread signal support.
        have_signals = False

    log.info("h_proxy ready with %d listener(s); press Ctrl+C to stop", len(servers))
    try:
        if have_signals:
            await stop.wait()
        else:
            # No signal handlers (e.g. Windows): wake periodically so a
            # KeyboardInterrupt from Ctrl+C is delivered promptly.
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
    finally:
        log.info("shutting down")
        for server in servers:
            server.close()
        await asyncio.gather(*(s.wait_closed() for s in servers), return_exceptions=True)


def run(config: Config) -> None:
    """Blocking entry point; translates Ctrl+C into a clean shutdown."""
    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:
        log.info("interrupted; exiting")
