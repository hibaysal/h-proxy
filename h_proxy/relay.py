"""Raw bidirectional byte relay shared by the TCP and HTTP proxies."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("h_proxy.relay")


async def _pump(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    buffer_size: int,
    idle_timeout: float | None,
) -> None:
    """Copy from ``reader`` to ``writer`` until EOF, then half-close ``writer``."""
    try:
        while True:
            if idle_timeout is not None:
                data = await asyncio.wait_for(reader.read(buffer_size), idle_timeout)
            else:
                data = await reader.read(buffer_size)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    except asyncio.TimeoutError:
        log.debug("relay idle timeout; closing direction")
    finally:
        # Signal EOF to the peer so a half-closed direction can still drain,
        # rather than tearing the whole connection down immediately.
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except (OSError, RuntimeError):
            pass


async def splice(
    a_reader: asyncio.StreamReader,
    a_writer: asyncio.StreamWriter,
    b_reader: asyncio.StreamReader,
    b_writer: asyncio.StreamWriter,
    buffer_size: int,
    idle_timeout: float | None = None,
) -> None:
    """Pump bytes both ways between two stream pairs until both directions end."""
    await asyncio.gather(
        _pump(a_reader, b_writer, buffer_size, idle_timeout),
        _pump(b_reader, a_writer, buffer_size, idle_timeout),
        return_exceptions=True,
    )
