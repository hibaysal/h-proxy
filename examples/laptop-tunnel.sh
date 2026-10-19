#!/usr/bin/env sh
# h_proxy — laptop side: publish the local SOCKS5 proxy on the home server.
# ===========================================================================
# Run h_proxy first so the SOCKS listener is up (127.0.0.1:1080), then run this
# to keep a reverse tunnel alive:  homeserver:<remote_port> -> laptop:<socks>.
# Uses autossh for auto-reconnect when available, otherwise plain ssh.
#
#   Usage: ./laptop-tunnel.sh [user@]homeserver [remote_port] [socks_port]
#   e.g.:  ./laptop-tunnel.sh me@home.example.com 6969 1080
#
# Then, on the home side, point Firefox / ssh at 127.0.0.1:<remote_port>
# (see examples/ssh_config). By default -R binds the remote port to loopback on
# the home server; to share it on the home LAN, enable `GatewayPorts
# clientspecified` in the home server's sshd_config, bind 0.0.0.0 below, and set
# `socks_auth` on the h_proxy listener.

set -eu

HOMESERVER="${1:?usage: laptop-tunnel.sh [user@]homeserver [remote_port] [socks_port]}"
REMOTE_PORT="${2:-6969}"
SOCKS_PORT="${3:-1080}"

# -R remote:localhost:socks exposes our SOCKS proxy at the home server.
# ExitOnForwardFailure makes ssh bail if the remote port is still held by a
# stale session, rather than silently running without the forward.
# (For LAN sharing, change "${REMOTE_PORT}" to "0.0.0.0:${REMOTE_PORT}".)
FORWARD="${REMOTE_PORT}:localhost:${SOCKS_PORT}"
OPTS="-N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes"

if command -v autossh >/dev/null 2>&1; then
    # shellcheck disable=SC2086  # intentional word-splitting of $OPTS
    exec autossh -M 0 $OPTS -R "$FORWARD" "$HOMESERVER"
else
    echo "autossh not found; using plain ssh (no auto-reconnect)" >&2
    # shellcheck disable=SC2086
    exec ssh $OPTS -R "$FORWARD" "$HOMESERVER"
fi
