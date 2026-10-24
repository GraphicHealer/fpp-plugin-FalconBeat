#!/bin/sh

# fpp-plugin-FalconBeat postStart — launch the sync daemon.
# Must return quickly (fppd startup blocks on us): nohup detached.
# stdout goes to /dev/null — the daemon logs through its own
# rotation-safe handler; an fd held open here would keep writing
# into the rotated .1 file after logrotate renames it.

. ${FPPDIR}/scripts/common

# Default is disabled — don't even start the daemon until the operator
# turns the plugin on (PLUGIN_GUIDELINES: inert until configured).
CFG="${MEDIADIR}/config/plugin.fpp-plugin-FalconBeat"
if ! grep -Eq '^enabled *= *"?1' "${CFG}" 2>/dev/null; then
    exit 0
fi

PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"

nohup /usr/bin/python3 "${PLUGIN_DIR}/falconbeatd.py" >/dev/null 2>&1 &
