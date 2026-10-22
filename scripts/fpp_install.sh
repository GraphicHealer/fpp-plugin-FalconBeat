#!/bin/bash
set -e

# fpp-plugin-FalconBeat install script

. ${FPPDIR}/scripts/common

PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Executables FPP/fppd will run directly or via scripts/eventScript
chmod +x "${PLUGIN_DIR}/callbacks.py" "${PLUGIN_DIR}/falconbeatd.py"
chmod +x "${PLUGIN_DIR}/scripts/"*.sh

# Plugin data dir (status.json, control socket, pidfile, secrets).
# install_plugin runs this script via sudo, so mkdir would leave the dir
# root:root 755 — and neither the daemon nor Apache (both run as fpp)
# could write their files. Match install_plugin's own chown convention.
mkdir -p "${MEDIADIR}/plugindata/fpp-plugin-FalconBeat"
chown -R fpp:fpp "${MEDIADIR}/plugindata/fpp-plugin-FalconBeat" 2>/dev/null || true

# Require an fppd restart: callbacks.py registers media/playlist hooks only at
# fppd start, and on upgrade a running falconbeatd keeps the OLD code until
# respawned. Set the flag through FPP's helper; fall back to the settings
# API on images whose common lacks it — never edit the settings file or
# restart fppd directly (PLUGIN_GUIDELINES §3.4/§3.6).
if command -v setSetting >/dev/null 2>&1; then
    setSetting restartFlag 1
else
    curl -s --max-time 5 -X PUT -d '1' \
        "http://localhost/api/settings/restartFlag" >/dev/null 2>&1 || true
fi
