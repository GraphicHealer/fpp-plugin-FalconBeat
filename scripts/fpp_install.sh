#!/bin/bash
set -e

# fpp-plugin-FalconBeat install script

. ${FPPDIR}/scripts/common

PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Executables FPP/fppd will run directly or via scripts/eventScript
chmod +x "${PLUGIN_DIR}/callbacks.py" "${PLUGIN_DIR}/falconbeatd.py"
chmod +x "${PLUGIN_DIR}/scripts/"*.sh

# Plugin data dir (status.json, control socket, pidfile)
mkdir -p "${MEDIADIR}/plugindata/fpp-plugin-FalconBeat"

# Require an fppd restart: callbacks.py registers media/playlist hooks only at
# fppd start, and on upgrade a running falconbeatd keeps the OLD code until
# respawned. restartFlag 2 = quick restart suffices (no reboot). Write it via
# the settings API so fppd's in-memory copy + the UI banner update now, with
# a direct settings-file write as fallback (fppd_restart clears it on restart).
if ! curl -s --max-time 5 -X PUT -d '2' \
        "http://localhost/api/settings/restartFlag" >/dev/null 2>&1; then
    if grep -q "^restartFlag" "${MEDIADIR}/settings" 2>/dev/null; then
        sed -i -e 's/^restartFlag .*/restartFlag = "2"/' "${MEDIADIR}/settings"
    else
        echo 'restartFlag = "2"' >> "${MEDIADIR}/settings"
    fi
fi
