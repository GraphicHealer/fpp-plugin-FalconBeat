#!/bin/sh

# fpp-plugin-FalconBeat postStart — launch the sync daemon.
# Must return quickly (fppd startup blocks on us): nohup into the plugin log.

. ${FPPDIR}/scripts/common

PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_LOG="${LOGDIR}/plugin-fpp-plugin-FalconBeat.log"

nohup /usr/bin/python3 "${PLUGIN_DIR}/falconbeatd.py" >> "${PLUGIN_LOG}" 2>&1 &
