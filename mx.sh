#!/bin/bash
# Run a command inside a running Mininet host from any terminal, e.g.
#   sudo topo/mx.sh c1 python3 chat/client.py --user vip_alice --password pw1234 --register
# Mininet starts each host as "bash --norc -is mininet:<name>"; mnexec -a
# attaches to that process's network namespace.
set -e
host=$1; shift
[ -n "$host" ] && [ $# -gt 0 ] || { echo "usage: sudo $0 HOST COMMAND..." >&2; exit 2; }
pid=$(pgrep -f "mininet:${host}\$" | head -1)
[ -n "$pid" ] || { echo "no running Mininet host '$host' (is topology.py running?)" >&2; exit 1; }
exec mnexec -a "$pid" "$@"
