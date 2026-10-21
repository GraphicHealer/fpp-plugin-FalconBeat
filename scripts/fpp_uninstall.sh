#!/bin/bash

# fpp-plugin-FalconBeat uninstall script.
# This runs BEFORE the plugin directory is deleted — stop the daemon and
# clean up everything we created outside the plugin directory.

. ${FPPDIR}/scripts/common

DATADIR="${MEDIADIR}/plugindata/fpp-plugin-FalconBeat"

# Stop the daemon if it is running
if [ -f "${DATADIR}/falconbeatd.pid" ]; then
    kill "$(cat "${DATADIR}/falconbeatd.pid")" 2>/dev/null || true
fi

# Socket, pidfile, status.json — all ours
rm -rf "${DATADIR}"

# The single plugin log is left for FPP to rotate/purge as usual.
