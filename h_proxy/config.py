"""Configuration loading for h_proxy.

The config file is YAML, but to stay dependency-free (standard library only)
we parse a small, well-defined *subset* of YAML rather than pulling in PyYAML.

Supported subset
----------------
* ``key: value`` mappings.
* Block lists whose items are either scalars (``- example.com``) or mappings
  (``- name: http`` followed by indented ``key: value`` lines).
* Nesting by indentation using spaces (tabs are rejected).
* ``#`` comments, either on their own line or trailing an unquoted value.
* Scalars: bare words parsed as bool / int / float / null / string, plus
  single- or double-quoted strings.

Anything fancier (flow style ``[a, b]`` / ``{a: b}``, anchors, multi-document
streams, block scalars ``|`` / ``>``) is intentionally *not* supported. The
example config stays well within the subset.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ConfigError(Exception):
    """Raised for any malformed configuration (syntax or semantics)."""


# --------------------------------------------------------------------------- #
# Minimal YAML-subset parser
# --------------------------------------------------------------------------- #

class _Line:
    __slots__ = ("indent", "text", "number")

    def __init__(self, indent: int, text: str, number: int) -> None:
        self.indent = indent
        self.text = text
        self.number = number


def _tokenize(text: str) -> list[_Line]:
    lines: list[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
            raise ConfigError(f"line {number}: tabs are not allowed for indentation")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in ("---", "..."):  # document markers: ignore
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        lines.append(_Line(indent, raw[indent:].rstrip(), number))
    return lines


def _looks_like_mapping_entry(s: str) -> bool:
    """True if ``s`` begins with ``key:`` where key is a plain scalar name."""
    idx = s.find(":")
    if idx <= 0:
        return False
    key = s[:idx]
    if not all(c.isalnum() or c in "_-." for c in key):
        return False
    # a real mapping colon is followed by end-of-line or whitespace,
    # which distinguishes "name: x" from "0.0.0.0:8080".
    return s[idx + 1 : idx + 2] in ("", " ")


def _split_key(s: str) -> tuple[str, str]:
    idx = s.find(":")
    return s[:idx].strip(), s[idx + 1 :].strip()


def _parse_scalar(s: str) -> Any:
    s = s.strip()
    if not s:
        return None
    if s[0] in ("'", '"'):
        quote = s[0]
        buf: list[str] = []
        i = 1
        closed = False
        while i < len(s):
            ch = s[i]
            if quote == '"' and ch == "\\" and i + 1 < len(s):
                nxt = s[i + 1]
                buf.append({'"': '"', "\\": "\\", "n": "\n", "t": "\t"}.get(nxt, nxt))
                i += 2
                continue
            if ch == quote:
                # '' inside single quotes is a literal quote
                if quote == "'" and i + 1 < len(s) and s[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                closed = True
                i += 1
                break
            buf.append(ch)
            i += 1
        if not closed:
            raise ConfigError(f"unterminated quoted string: {s}")
        rest = s[i:].strip()  # only whitespace or a trailing comment may follow
        if rest and not rest.startswith("#"):
            raise ConfigError(f"unexpected text after quoted string: {rest}")
        return "".join(buf)
    # strip a trailing inline comment on bare scalars (" # ...").
    hash_at = s.find(" #")
    if hash_at != -1:
        s = s[:hash_at].strip()
    low = s.lower()
    if low in ("null", "~", "none", ""):
        return None
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _parse_block(lines: list[_Line], i: int, indent: int) -> tuple[Any, int]:
    if lines[i].text.startswith("-"):
        return _parse_list(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_map(lines: list[_Line], i: int, indent: int) -> tuple[dict, int]:
    result: dict[str, Any] = {}
    while i < len(lines) and lines[i].indent == indent:
        line = lines[i]
        if line.text.startswith("- "):
            raise ConfigError(f"line {line.number}: unexpected list item in mapping")
        if not _looks_like_mapping_entry(line.text) and ":" not in line.text:
            raise ConfigError(f"line {line.number}: expected 'key: value'")
        key, remainder = _split_key(line.text)
        if not key:
            raise ConfigError(f"line {line.number}: empty key")
        i += 1
        if remainder == "":
            value, i = _parse_nested(lines, i, indent)
        else:
            value = _parse_scalar(remainder)
        result[key] = value
    return result, i


def _parse_list(lines: list[_Line], i: int, indent: int) -> tuple[list, int]:
    result: list[Any] = []
    while i < len(lines) and lines[i].indent == indent and lines[i].text[:1] == "-":
        line = lines[i]
        rest = line.text[1:]
        stripped = rest.lstrip(" ")
        content_col = indent + 1 + (len(rest) - len(stripped))
        if stripped == "":
            # value lives on the following, more-indented lines
            i += 1
            value, i = _parse_nested(lines, i, indent)
            result.append(value)
        elif _looks_like_mapping_entry(stripped):
            value, i = _parse_inline_map(lines, i, content_col, stripped)
            result.append(value)
        else:
            result.append(_parse_scalar(stripped))
            i += 1
    return result, i


def _parse_inline_map(
    lines: list[_Line], i: int, col: int, first_text: str
) -> tuple[dict, int]:
    """Parse ``- key: value`` list items whose extra keys align under ``col``."""
    result: dict[str, Any] = {}
    key, remainder = _split_key(first_text)
    i += 1
    if remainder == "":
        value, i = _parse_nested(lines, i, col)
        result[key] = value
    else:
        result[key] = _parse_scalar(remainder)
    while (
        i < len(lines)
        and lines[i].indent == col
        and not lines[i].text.startswith("- ")
    ):
        line = lines[i]
        if not _looks_like_mapping_entry(line.text):
            raise ConfigError(f"line {line.number}: expected 'key: value'")
        key, remainder = _split_key(line.text)
        i += 1
        if remainder == "":
            value, i = _parse_nested(lines, i, col)
            result[key] = value
        else:
            result[key] = _parse_scalar(remainder)
    return result, i


def _parse_nested(lines: list[_Line], i: int, parent_indent: int) -> tuple[Any, int]:
    if i < len(lines) and lines[i].indent > parent_indent:
        return _parse_block(lines, i, lines[i].indent)
    # list items may be indented at the same column as their '-'
    if i < len(lines) and lines[i].text.startswith("-") and lines[i].indent >= parent_indent:
        return _parse_block(lines, i, lines[i].indent)
    return None, i


def parse_yaml(text: str) -> Any:
    """Parse the supported YAML subset into Python objects."""
    lines = _tokenize(text)
    if not lines:
        return None
    value, i = _parse_block(lines, 0, lines[0].indent)
    if i != len(lines):
        raise ConfigError(f"line {lines[i].number}: unexpected indentation")
    return value


# --------------------------------------------------------------------------- #
# Address helpers
# --------------------------------------------------------------------------- #

def parse_address(spec: Any, *, default_host: str | None, what: str) -> tuple[str, int]:
    """Parse ``host:port`` (or ``:port`` / ``[ipv6]:port``) into a tuple."""
    if not isinstance(spec, str):
        raise ConfigError(f"{what}: expected a 'host:port' string, got {spec!r}")
    s = spec.strip()
    if s.startswith("["):  # [ipv6]:port
        host, sep, rest = s[1:].partition("]")
        if not sep or not rest.startswith(":"):
            raise ConfigError(f"{what}: malformed IPv6 address {spec!r} (use [addr]:port)")
        port_str = rest[1:]
    elif s.count(":") >= 2:
        raise ConfigError(
            f"{what}: IPv6 address {spec!r} must be written as [addr]:port"
        )
    else:
        host, sep, port_str = s.rpartition(":")
        if not sep:
            raise ConfigError(f"{what}: expected 'host:port', got {spec!r}")
    if host == "":
        if default_host is None:
            raise ConfigError(f"{what}: a host is required in {spec!r}")
        host = default_host
    try:
        port = int(port_str)
    except ValueError:
        raise ConfigError(f"{what}: invalid port in {spec!r}") from None
    if not (0 < port < 65536):
        raise ConfigError(f"{what}: port out of range in {spec!r}")
    return host, port


# --------------------------------------------------------------------------- #
# Config dataclasses
# --------------------------------------------------------------------------- #

VALID_MODES = ("tcp", "http")


@dataclass
class ListenerConfig:
    name: str
    mode: str
    listen_host: str
    listen_port: int
    backend_host: str
    backend_port: int
    # http-only options
    forwarded_headers: bool = True
    host: str | None = None  # optional Host allow-value; others get 404
    # L4 options (apply to both modes)
    send_proxy: str | None = None  # None | "v1" | "v2": PROXY protocol to backend
    source: str | None = None  # local source IP to bind outbound (haproxy 'source')

    @property
    def listen_addr(self) -> str:
        return f"{self.listen_host}:{self.listen_port}"

    @property
    def backend_addr(self) -> str:
        return f"{self.backend_host}:{self.backend_port}"


@dataclass
class Config:
    listeners: list[ListenerConfig] = field(default_factory=list)
    connect_timeout: float = 10.0
    buffer_size: int = 65536
    idle_timeout: float | None = None  # seconds; None disables
    max_header_bytes: int = 1 << 20  # 1 MiB of request/response head
    max_connections: int | None = None  # cap on concurrent connections; None = unlimited


_KNOWN_LISTENER_KEYS = {
    "name",
    "mode",
    "listen",
    "backend",
    "forwarded_headers",
    "host",
    "send_proxy",
    "source",
}
_KNOWN_TOP_KEYS = {
    "listeners",
    "connect_timeout",
    "buffer_size",
    "idle_timeout",
    "max_header_bytes",
    "max_connections",
}


def _require(mapping: dict, key: str, where: str) -> Any:
    if key not in mapping or mapping[key] is None:
        raise ConfigError(f"{where}: missing required key '{key}'")
    return mapping[key]


def _build_listener(raw: Any, index: int) -> ListenerConfig:
    where = f"listener #{index + 1}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a mapping of options")
    unknown = set(raw) - _KNOWN_LISTENER_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown option(s): {', '.join(sorted(unknown))}")

    name = str(_require(raw, "name", where))
    where = f"listener '{name}'"

    mode = str(_require(raw, "mode", where)).lower()
    if mode not in VALID_MODES:
        raise ConfigError(f"{where}: mode must be one of {VALID_MODES}, got {mode!r}")

    listen_host, listen_port = parse_address(
        _require(raw, "listen", where), default_host="0.0.0.0", what=f"{where} listen"
    )
    backend_host, backend_port = parse_address(
        _require(raw, "backend", where), default_host=None, what=f"{where} backend"
    )

    forwarded = raw.get("forwarded_headers", True)
    if not isinstance(forwarded, bool):
        raise ConfigError(f"{where}: forwarded_headers must be true/false")

    host = raw.get("host")
    if host is not None:
        host = str(host)
        if mode != "http":
            raise ConfigError(f"{where}: 'host' matching only applies to http mode")

    send_proxy = raw.get("send_proxy")
    if send_proxy in (None, False):
        send_proxy = None
    elif send_proxy is True or send_proxy == "v2":
        send_proxy = "v2"
    elif send_proxy == "v1":
        send_proxy = "v1"
    else:
        raise ConfigError(f"{where}: send_proxy must be one of false, v1, v2")

    source = raw.get("source")
    if source is not None:
        source = str(source).strip()
        if not source:
            raise ConfigError(f"{where}: source must be a non-empty IP address")

    return ListenerConfig(
        name=name,
        mode=mode,
        listen_host=listen_host,
        listen_port=listen_port,
        backend_host=backend_host,
        backend_port=backend_port,
        forwarded_headers=forwarded,
        host=host,
        send_proxy=send_proxy,
        source=source,
    )


def from_dict(data: Any) -> Config:
    """Validate a parsed document and build a :class:`Config`."""
    if not isinstance(data, dict):
        raise ConfigError("top level of config must be a mapping")
    unknown = set(data) - _KNOWN_TOP_KEYS
    if unknown:
        raise ConfigError(f"unknown top-level option(s): {', '.join(sorted(unknown))}")

    raw_listeners = data.get("listeners")
    if not isinstance(raw_listeners, list) or not raw_listeners:
        raise ConfigError("config must define a non-empty 'listeners' list")

    listeners = [_build_listener(item, i) for i, item in enumerate(raw_listeners)]

    names = [l.name for l in listeners]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"duplicate listener name(s): {', '.join(sorted(dupes))}")

    bindings = [l.listen_addr for l in listeners]
    dupe_binds = {b for b in bindings if bindings.count(b) > 1}
    if dupe_binds:
        raise ConfigError(f"multiple listeners bind the same address: {', '.join(sorted(dupe_binds))}")

    cfg = Config(listeners=listeners)

    def _positive_number(key: str, current: float) -> float:
        if key not in data:
            return current
        value = data[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"'{key}' must be a positive number")
        return float(value)

    cfg.connect_timeout = _positive_number("connect_timeout", cfg.connect_timeout)
    cfg.buffer_size = int(_positive_number("buffer_size", cfg.buffer_size))
    cfg.max_header_bytes = int(_positive_number("max_header_bytes", cfg.max_header_bytes))
    if "idle_timeout" in data and data["idle_timeout"] is not None:
        cfg.idle_timeout = _positive_number("idle_timeout", cfg.idle_timeout or 0.0)
    if "max_connections" in data and data["max_connections"] is not None:
        mc = data["max_connections"]
        if not isinstance(mc, int) or isinstance(mc, bool) or mc <= 0:
            raise ConfigError("'max_connections' must be a positive integer")
        cfg.max_connections = mc

    return cfg


def loads(text: str) -> Config:
    return from_dict(parse_yaml(text))


def load(path: str) -> Config:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path!r}: {exc}") from None
    return loads(text)
