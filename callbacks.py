#!/usr/bin/env python3
"""
FPPD callback entry point for fpp-plugin-FalconBeat.

FPPD calls this via scripts/eventScript:

    callbacks.py --list                        -> comma list of callback types
    callbacks.py --type media     --data '<json>'
    callbacks.py --type playlist  --data '<json>'
    callbacks.py --type lifecycle startup|shutdown

These calls are synchronous from fppd's point of view (it waitpid()s), so this
script must return fast: it forwards the event to falconbeatd over its unix
control socket and exits. If the daemon is down it is (re)spawned first.
"""

import json
import os
import socket
import subprocess
import sys
import time

PLUGIN_NAME = "fpp-plugin-FalconBeat"
MEDIADIR = os.environ.get("MEDIADIR", "/home/fpp/media")
LOGDIR = os.environ.get("LOGDIR", os.path.join(MEDIADIR, "logs"))
PLUGINDIR = os.path.dirname(os.path.abspath(__file__))
DATADIR = os.path.join(MEDIADIR, "plugindata", PLUGIN_NAME)
HAS_UNIX = hasattr(socket, "AF_UNIX")
SOCK_PATH = os.path.join(
    DATADIR, "falconbeatd.sock" if HAS_UNIX else "falconbeatd.port"
)
DAEMON = os.path.join(PLUGINDIR, "falconbeatd.py")
SETTINGS_PATH = os.path.join(
    MEDIADIR, "config", "plugin." + PLUGIN_NAME
)
DEFAULT_GATE_SEC = 10.0
LOG_FILE = os.path.join(LOGDIR, "plugin-%s.log" % PLUGIN_NAME)
PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable


def gate_sec():
    """How long a media callback may hold the track start waiting for the
    daemon's ready reply (fppd blocks in waitpid inside PreparePlay, so
    returning releases the gate and the track starts). 0 = don't gate."""
    try:
        with open(SETTINGS_PATH) as f:
            for line in f:
                k, _, v = line.partition("=")
                if k.strip() == "gateMaxSec":
                    v = v.strip().strip('"')
                    return float(v) if v else 0.0
    except (OSError, ValueError):
        pass
    return DEFAULT_GATE_SEC


def control_connect(timeout=5):
    if HAS_UNIX:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(SOCK_PATH)
        return s
    with open(SOCK_PATH) as f:
        host, port = f.read().strip().rsplit(":", 1)
    return socket.create_connection((host, int(port)), timeout=timeout)


def log(msg):
    try:
        os.makedirs(LOGDIR, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write("%s callbacks: %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    except OSError:
        pass


def send_to_daemon(payload, want_reply=False, timeout=5):
    s = control_connect(timeout)
    s.sendall(json.dumps(payload).encode("utf-8"))
    s.shutdown(socket.SHUT_WR)
    reply = b""
    if want_reply:
        try:
            reply = s.recv(4096)
        except OSError:
            pass
    s.close()
    return reply


def daemon_alive():
    try:
        return b'"ok":true' in send_to_daemon({"cb_type": "ping"}, want_reply=True)
    except OSError:
        return False


def start_daemon():
    """Spawn falconbeatd detached, appending to the plugin log."""
    os.makedirs(DATADIR, exist_ok=True)
    try:
        f = open(LOG_FILE, "ab", buffering=0)
    except OSError:
        f = subprocess.DEVNULL
    subprocess.Popen(
        [PY, DAEMON],
        stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True, cwd=PLUGINDIR,
    )
    # wait for the control socket to accept
    for _ in range(50):
        if daemon_alive():
            return True
        time.sleep(0.1)
    log("daemon did not come up within 5s")
    return False


def ensure_daemon():
    if daemon_alive():
        return True
    return start_daemon()


def main(argv):
    args = argv[1:]
    if "--list" in args:
        print("media,playlist,lifecycle")
        return 0

    cb_type = None
    data_arg = None
    for i, a in enumerate(args):
        if a == "--type" and i + 1 < len(args):
            cb_type = args[i + 1]
        elif a == "--data" and i + 1 < len(args):
            data_arg = args[i + 1]

    if cb_type == "lifecycle":
        name = data_arg or (args[-1] if args else "")
        # lifecycle passes the phase as a bare arg: --type lifecycle startup
        if name in ("startup", "shutdown"):
            if name == "startup":
                ensure_daemon()
            else:
                try:
                    send_to_daemon({"cb_type": "shutdown"})
                except OSError:
                    pass
            return 0

    if cb_type in ("media", "playlist"):
        try:
            data = json.loads(data_arg) if data_arg else {}
        except ValueError:
            data = {"_raw": data_arg}
        if not ensure_daemon():
            log("daemon unavailable; dropped %s event" % cb_type)
            return 1
        # Media callbacks are blocking inside fppd's PreparePlay — before the
        # media output exists. Holding our reply until the daemon finishes
        # push + PLAY means the track can't start before phones are ready.
        gate = gate_sec() if cb_type == "media" else 0.0
        payload = {"cb_type": cb_type, "data": data}
        if gate > 0:
            payload["wait"] = True
        try:
            reply = send_to_daemon(
                payload, want_reply=(gate > 0), timeout=(gate if gate > 0 else 5)
            )
        except OSError as e:
            log("failed to forward %s event: %s" % (cb_type, e))
            return 1
        if gate > 0:
            if not reply:
                log("sync gate: no daemon reply within %.1fs — released" % gate)
            elif b'"ok":false' in reply:
                log("sync gate: daemon reported not-ready — released")
        return 0

    # Unknown invocation — answer --list-style so fppd just ignores us
    if "--list" in argv:
        print("media,playlist,lifecycle")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
