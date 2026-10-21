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

# No restartFlag: a callbacks script is reloaded live on install (PLUGIN_GUIDELINES.md §3.6).
