#!/bin/sh

# fpp-plugin-FalconBeat preStop — stop the daemon we launched in postStart.

. ${FPPDIR}/scripts/common

DATADIR="${MEDIADIR}/plugindata/fpp-plugin-FalconBeat"

if [ -f "${DATADIR}/falconbeatd.pid" ]; then
    kill "$(cat "${DATADIR}/falconbeatd.pid")" 2>/dev/null || true
fi
