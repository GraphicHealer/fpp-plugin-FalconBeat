#!/usr/bin/env python3
"""
FalconBeat daemon — keeps FPP playback mirrored to a FalconBeat room.

Run by FPP (postStart.sh / lifecycle callback). Speaks:

  - FalconBeat admin WS (ws://<server>/ws?roomId=.. — the shared token
    rides the Authorization: Bearer upgrade header, not the query string)
    held open for the duration of a playlist so the room (deleted after
    60s idle) stays alive and listeners stay attached.
  - FalconBeat HTTP     (POST /upload/complete — Bearer-gated at the edge)
  - FalconBeat ingest  (PUT/DELETE <server>/media/<name>, PUT /config/*,
    Bearer token) — all through the one published port
  - FPP status API     (GET http://127.0.0.1/api/fppd/status — position)
  - Local control      (unix socket: callbacks.py forwards fppd events)

Protocol reference: FalconBeat-Server/bridge/src/client.ts and
FalconBeat-Server/beatsync/packages/shared/types/*.ts (@ 94c38ab).

Stdlib only — FPP images ship python3; pip installs are system-wide and
shared across plugins, so we deliberately keep zero dependencies.
"""

import base64
import hashlib
import json
import logging
import logging.handlers
import os
import re
import select
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import fcntl
except ImportError:
    fcntl = None

PLUGIN_NAME = "fpp-plugin-FalconBeat"
MEDIADIR = os.environ.get("MEDIADIR", "/home/fpp/media")
CFGDIR = os.environ.get("CFGDIR", os.path.join(MEDIADIR, "config"))
LOGDIR = os.environ.get("LOGDIR", os.path.join(MEDIADIR, "logs"))
PLUGINDIR = os.environ.get(
    "FPP_PLUGIN_DIR", os.path.join(MEDIADIR, "plugins", PLUGIN_NAME)
)
DATADIR = os.path.join(MEDIADIR, "plugindata", PLUGIN_NAME)
HAS_UNIX = hasattr(socket, "AF_UNIX")
# Unix socket on Linux/FPP; localhost TCP (port written to the file) elsewhere.
SOCK_PATH = os.path.join(
    DATADIR, "falconbeatd.sock" if HAS_UNIX else "falconbeatd.port"
)
PID_PATH = os.path.join(DATADIR, "falconbeatd.pid")
LOCK_PATH = os.path.join(DATADIR, "falconbeatd.lock")
STATE_PATH = os.path.join(DATADIR, "status.json")
SECRETS_PATH = os.path.join(DATADIR, "secrets")
SETTINGS_PATH = os.path.join(CFGDIR, "plugin." + PLUGIN_NAME)
AUDIO_DIR = os.path.join(DATADIR, "audio")  # extracted video soundtracks
FPP_STATUS_URL = "http://127.0.0.1/api/fppd/status"
FPP_SYSINFO_URL = "http://127.0.0.1/api/system/info"

LOG = logging.getLogger("falconbeatd")


# --------------------------------------------------------------------------
# Settings (config/plugin.fpp-plugin-FalconBeat — FPP key=value format)
# --------------------------------------------------------------------------

DEFAULTS = {
    # Off until configured — a fresh install must not touch the network
    # (PLUGIN_GUIDELINES §14.13).
    "enabled": "0",
    # LAN address of the FalconBeat edge — one published port carries the
    # admin WS, ingest writes, and the gated HTTP endpoints. Accepts
    # "host", "host:port", or "http(s)://host[:port]". Empty = unconfigured.
    "serverAddr": "",
    # Fixed room — the listener page joins 000000 (its ?room= override is
    # for the server owner's own testing, not a plugin setting).
    "roomId": "000000",
    # Shared token: Authorization header on the admin WS upgrade AND Bearer
    # on /upload/complete + ingest writes — the server's FALCONBEAT_TOKEN.
    "falconbeatToken": "",
    # Extra milliseconds added to the computed schedule delay when telling
    # devices where in the track to start. Tune if devices run audibly late.
    "nudgeMs": "0",
    # Seconds after a playlist stop before the admin WS drops (room GC'd ~60s
    # later regardless). Small grace lets listeners see the PAUSE land.
    "disconnectGraceSec": "5",
    # Added to the track position devices start at (the PLAY
    # trackTimeSeconds). Positive = devices land further into the song —
    # raise it if the audio trails the lights, lower it if it runs ahead.
    "syncOffsetMs": "0",
}

_settings_cache = {}


def _settings_sig():
    sig = []
    for p in (SETTINGS_PATH, SECRETS_PATH):
        try:
            st = os.stat(p)
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append(None)
    return tuple(sig)


def load_settings():
    # The reconcile loop calls this every few seconds; re-parse only when a
    # file actually changed (mtime+size), otherwise hand back the cache.
    sig = _settings_sig()
    if sig == _settings_cache.get("sig") and _settings_cache.get("cfg"):
        return dict(_settings_cache["cfg"])
    settings = dict(DEFAULTS)
    # config/plugin.* first, then plugindata/secrets (0600) on top —
    # credentials live outside config/ so backups and crash bundles
    # never carry them (PLUGIN_GUIDELINES §14.4/§14.11). A key still in
    # plugin.* (pre-migration save) is used when secrets lacks it.
    for path in (SETTINGS_PATH, SECRETS_PATH):
        try:
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip()
                    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                        value = value[1:-1]
                    settings[key] = value
        except FileNotFoundError:
            pass
        except OSError as e:
            LOG.warning("cannot read %s: %s", path, e)
    # Derive the wire URLs from the simple fields. One edge port carries
    # everything, so httpBase serves beatsync HTTP, ingest writes, and the
    # gated endpoints alike.
    secure, hostport = _split_addr(settings.get("serverAddr", ""))
    settings["httpBase"] = ("https" if secure else "http") + "://" + hostport
    settings["wsBase"] = ("wss" if secure else "ws") + "://" + hostport
    _settings_cache.clear()
    _settings_cache.update({"sig": sig, "cfg": settings})
    return settings


def configured(cfg):
    """True only when the plugin is enabled AND has enough to reach the
    server — until then nothing runs, polls, or sends (§14.13)."""
    return (
        cfg_bool(cfg.get("enabled", "0"))
        and bool((cfg.get("serverAddr") or "").strip())
        and bool((cfg.get("falconbeatToken") or "").strip())
    )


def _split_addr(addr):
    """'host' | 'host:port' | scheme://host[:port] -> (secure, hostport)."""
    addr = addr.strip().rstrip("/")
    if "://" in addr:
        u = urllib.parse.urlsplit(addr)
        return u.scheme in ("https", "wss"), (u.netloc or u.path)
    return False, addr


def cfg_bool(s):
    return str(s).strip().lower() not in ("0", "false", "no", "off", "")


# --------------------------------------------------------------------------
# Minimal WebSocket client (RFC 6455, client side, ws:// and wss://)
# --------------------------------------------------------------------------


class WSClosed(Exception):
    pass


class MiniWS:
    """Just enough WebSocket for a LAN control channel: text frames in/out,
    ping->pong, close. Server->client frames are unmasked; we mask ours as
    required."""

    def __init__(self, url, timeout=10, headers=None):
        self.url = url
        self.sock, self._recvbuf = self._handshake(url, timeout, headers)
        self._recvbuf = bytearray(self._recvbuf)
        self._send_lock = threading.Lock()
        self.sock.settimeout(None)

    @staticmethod
    def _handshake(url, timeout, headers=None):
        """Returns (socket, leftover bytes read past the response headers)."""
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("ws", "wss"):
            raise ValueError("unsupported WS scheme: %r" % u.scheme)
        host = u.hostname or "localhost"
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        raw = socket.create_connection((host, port), timeout=timeout)
        if u.scheme == "wss":
            raw = ssl.create_default_context().wrap_socket(
                raw, server_hostname=host
            )

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        extra = "".join(
            "%s: %s\r\n" % (k, v) for k, v in (headers or {}).items()
        )
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "%s"
            "\r\n" % (path, host, port, key, extra)
        )
        raw.sendall(req.encode("ascii"))

        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = raw.recv(4096)
            if not chunk:
                raw.close()
                raise WSClosed("handshake: connection closed")
            resp += chunk
            if len(resp) > 16384:
                raw.close()
                raise WSClosed("handshake: oversized response")
        head, _, rest = resp.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 101" not in status:
            raw.close()
            raise WSClosed("handshake rejected: %s" % status.decode("latin1"))
        # Any bytes after the header terminator belong to the first frame.
        return raw, rest

    def _read_exact(self, n):
        while len(self._recvbuf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSClosed("connection closed")
            self._recvbuf += chunk
        out = bytes(self._recvbuf[:n])
        del self._recvbuf[:n]
        return out

    def _send_frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(bytes(header) + masked)

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def send_pong(self, payload=b""):
        self._send_frame(0xA, payload)

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            # shutdown() wakes a recv blocked in another thread; close() alone
            # may not on Linux
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def recv_message(self):
        """Return the next complete text message as str, or None on close.
        Handles fragmentation; answers pings."""
        payload = bytearray()
        while True:
            b1, b2 = self._read_exact(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                (length,) = struct.unpack(">H", self._read_exact(2))
            elif length == 127:
                (length,) = struct.unpack(">Q", self._read_exact(8))
            mask = self._read_exact(4) if masked else b""
            data = self._read_exact(length) if length else b""
            if masked:
                data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))

            if opcode == 0x8:  # close
                raise WSClosed("close frame received")
            if opcode == 0x9:  # ping
                self.send_pong(data)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x1, 0x0):  # text / continuation
                payload += data
                if fin:
                    return payload.decode("utf-8", "replace")
                continue
            # binary/other: ignore


# --------------------------------------------------------------------------
# FalconBeat session: persistent admin WS + push/register/play/pause
# --------------------------------------------------------------------------


def epoch_ms():
    return int(time.time() * 1000)


def http_request(method, url, body=None, headers=None, timeout=15):
    """Plain HTTP helper (stdlib). Returns (status, response bytes)."""
    req = urllib.request.Request(url, method=method, data=body)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


class FalconBeatSession:
    """One admin WS connection to the FalconBeat server, with background reader,
    NTP probing (coded probe pairs per shared/types), liveness replies, and
    SCHEDULED_ACTION echo matching for PLAY/PAUSE confirmation."""

    # Coded probes: 25ms apart within a pair; fast start every 50ms for the
    # first 16 measurements, steady state every 2.5s (see AGENTS.md notes).
    NTP_PAIR_GAP = 0.025
    NTP_FAST_PERIOD = 0.05
    NTP_STEADY_PERIOD = 2.5
    NTP_FAST_COUNT = 16

    def __init__(self, cfg, on_event=None):
        self.cfg = cfg
        self.on_event = on_event or (lambda msg: None)
        self.ws = None
        self._reader = None
        self._ntp_thread = None
        self._closed = threading.Event()
        self._connected = threading.Event()
        self._echo_cond = threading.Condition()
        self._echo_waiters = []  # list of dicts {type, audioSource, result}
        self._send_lock = threading.Lock()
        # timing state
        self._lock = threading.Lock()
        self.clock_offset_ms = 0.0  # server_epoch - local_epoch
        self.best_rtt_ms = None
        self.max_client_rtt_ms = 0.0
        self.max_client_comp_ms = 0.0
        self.listener_count = 0  # non-admin clients (devices), not our socket
        self.registered_urls = set()  # sources the room knows (SET_AUDIO_SOURCES)
        self._ntp_req = {}  # probeGroupId -> [t0_first, t0_second]
        self._ntp_seq = 0
        self._gen = 0  # connection generation; stale threads exit when it moves
        # Does this server honor executeAtServerTime? None until the first
        # SCHEDULED_ACTION echo proves it (echo returns the honored stamp).
        self.execat_supported = None

    # -- connection ----------------------------------------------------

    def connect(self):
        if self._connected.is_set():
            return
        # drop any dead socket from a previous attempt
        if self.ws is not None:
            try:
                self.ws.close()
            except OSError:
                pass
            self.ws = None
        self.http_base = self.cfg["httpBase"]
        ws_base = self.cfg["wsBase"]
        params = {
            "roomId": self.cfg["roomId"],
            "username": "falconbeat-fpp",
            "clientId": "fpp-%d" % os.getpid(),
        }
        url = "%s/ws?%s" % (ws_base, urllib.parse.urlencode(params))
        # Token rides the upgrade header — a ?creator= query lands in every
        # proxy's access log on the way to beatsync.
        headers = {}
        if self.cfg.get("falconbeatToken"):
            headers["Authorization"] = (
                "Bearer %s" % self.cfg["falconbeatToken"]
            )
        self.ws = MiniWS(url, headers=headers)
        self._closed.clear()
        self._gen += 1
        gen = self._gen
        self._reader = threading.Thread(
            target=self._read_loop, args=(gen,), daemon=True
        )
        self._reader.start()
        self._ntp_thread = threading.Thread(
            target=self._ntp_loop, args=(gen,), daemon=True
        )
        self._ntp_thread.start()
        self._connected.set()
        LOG.info("FalconBeat WS connected: %s room %s", ws_base, self.cfg["roomId"])

    def close(self):
        self._closed.set()
        self._connected.clear()
        if self.ws:
            try:
                self.ws.close()
            except OSError:
                pass
            self.ws = None
        with self._echo_cond:
            for w in self._echo_waiters:
                w["result"] = WSClosed("session closed")
            self._echo_cond.notify_all()

    # -- reader / dispatch ----------------------------------------------

    def _read_loop(self, gen):
        ws = self.ws  # bind to this connection; a reconnect swaps self.ws out
        try:
            while not self._closed.is_set() and gen == self._gen:
                raw = ws.recv_message()
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                self._dispatch(msg)
        except (WSClosed, OSError) as e:
            LOG.info("WS reader ended: %s", e)
        finally:
            if gen == self._gen:
                self._connected.clear()
                self.on_event({"type": "_disconnected"})

    def _dispatch(self, msg):
        t = msg.get("type")
        if t == "LIVENESS_PING":
            self._send({"type": "LIVENESS_PONG"})
            return
        if t == "NTP_RESPONSE":
            self._handle_ntp(msg)
            return
        if t == "SCHEDULED_ACTION":
            self._match_echo(msg)
            return
        if t == "ROOM_EVENT":
            ev = msg.get("event", {})
            if ev.get("type") == "CLIENT_CHANGE":
                self._track_client_stats(ev.get("clients", []))
            elif ev.get("type") == "SET_AUDIO_SOURCES":
                with self._lock:
                    self.registered_urls = {
                        s.get("url") for s in ev.get("sources", [])
                    }
            elif ev.get("type") == "LOAD_AUDIO_SOURCE":
                # We have nothing to load — we're the source — but the
                # coordination counts every room client; ACK immediately so
                # the scheduled play isn't held up on our account.
                url = (ev.get("audioSourceToPlay") or {}).get("url")
                if url:
                    self._send(
                        {"type": "AUDIO_SOURCE_LOADED", "source": {"url": url}}
                    )
            self.on_event(msg)
            return
        self.on_event(msg)

    def _track_client_stats(self, clients):
        with self._lock:
            for c in clients:
                self.max_client_rtt_ms = max(
                    self.max_client_rtt_ms, float(c.get("rtt", 0))
                )
                self.max_client_comp_ms = max(
                    self.max_client_comp_ms, float(c.get("compensationMs", 0))
                )
            # Listener count = non-admin clients (our creator socket is admin)
            self.listener_count = sum(1 for c in clients if not c.get("isAdmin"))

    # -- NTP (coded probe pairs; keep min-RTT sample's offset) ----------

    def _ntp_loop(self, gen):
        n = 0
        while not self._closed.is_set() and gen == self._gen:
            group = self._ntp_seq = self._ntp_seq + 1
            for idx in (0, 1):
                if self._closed.is_set() or gen != self._gen:
                    return
                try:
                    self._send(
                        {
                            "type": "NTP_REQUEST",
                            "t0": epoch_ms(),
                            "probeGroupId": group,
                            "probeGroupIndex": idx,
                        }
                    )
                except WSClosed:
                    return
                if idx == 0:
                    time.sleep(self.NTP_PAIR_GAP)
            n += 1
            period = (
                self.NTP_FAST_PERIOD if n < self.NTP_FAST_COUNT
                else self.NTP_STEADY_PERIOD
            )
            self._closed.wait(period)

    def _handle_ntp(self, msg):
        try:
            t0 = int(msg["t0"])
            t1 = int(msg["t1"])
            t2 = int(msg["t2"])
        except (KeyError, TypeError, ValueError):
            return
        t3 = epoch_ms()
        rtt = (t3 - t0) - (t2 - t1)
        offset = ((t1 - t0) + (t2 - t3)) / 2.0
        with self._lock:
            if self.best_rtt_ms is None or rtt < self.best_rtt_ms:
                self.best_rtt_ms = rtt
                self.clock_offset_ms = offset

    def expected_delay_s(self):
        """Mirror the server's schedule-delay formula:
        max(400ms, 1.5*maxClientRTT+200, maxClientCompensation+200), cap 3s."""
        with self._lock:
            rtt = self.max_client_rtt_ms
            comp = self.max_client_comp_ms
            own = self.best_rtt_ms or 0
        delay = max(400.0, 1.5 * max(rtt, own) + 200.0, comp + 200.0)
        delay = min(delay, 3000.0)
        delay += float(self.cfg.get("nudgeMs", "0") or 0)
        return delay / 1000.0

    def server_now_ms(self):
        with self._lock:
            return epoch_ms() + self.clock_offset_ms

    # -- requests --------------------------------------------------------

    def _send(self, obj):
        if self.ws is None:
            raise WSClosed("not connected")
        self.ws.send_text(json.dumps(obj))

    def send_command(self, request, timeout=8.0):
        """Send PLAY/PAUSE and wait for the SCHEDULED_ACTION echo — the only
        positive confirmation (rejections are swallowed server-side)."""
        waiter = {
            "type": request["type"],
            "audioSource": request.get("audioSource"),
            # Two PLAYs for one source (pre-schedule + refine) each echo —
            # match on executeAt too so a stale echo can't satisfy the
            # newer waiter.
            "executeAt": request.get("executeAtServerTime"),
            "result": None,
        }
        with self._echo_cond:
            self._echo_waiters.append(waiter)
        try:
            self._send(request)
        except Exception:
            with self._echo_cond:
                self._echo_waiters.remove(waiter)
            raise
        deadline = time.monotonic() + timeout
        with self._echo_cond:
            while waiter["result"] is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    self._echo_waiters.remove(waiter)
                    raise TimeoutError(
                        "no SCHEDULED_ACTION echo for %s — rejected? "
                        "(unknown audio source? not admin?)" % request["type"]
                    )
                self._echo_cond.wait(left)
        result = waiter["result"]
        if isinstance(result, Exception):
            raise result
        return result

    def _match_echo(self, msg):
        action = msg.get("scheduledAction", {}) or {}
        with self._echo_cond:
            for w in self._echo_waiters:
                if w["type"] != action.get("type"):
                    continue
                if w["audioSource"] and action.get("audioSource") != w["audioSource"]:
                    continue
                if w["executeAt"] is not None and \
                        action.get("executeAtServerTime") != w["executeAt"]:
                    continue
                w["result"] = msg
            self._echo_cond.notify_all()
            self._echo_waiters = [w for w in self._echo_waiters
                                  if w["result"] is None]

    def send_play(self, url, pos, exec_ms=None, timeout=8.0):
        """PLAY url at track position pos. exec_ms pins the absolute server
        time the action executes at — ignored (stripped) by servers without
        the executeAtServerTime patch, which fall back to now+dynamicDelay."""
        req = {
            "type": "PLAY",
            "audioSource": url,
            "trackTimeSeconds": pos,
        }
        if exec_ms is not None:
            req["executeAtServerTime"] = int(exec_ms)
        echo = self.send_command(req, timeout)
        self._negotiate_execat(exec_ms, echo)
        return echo

    def send_pause(self, url, pos, exec_ms=None, timeout=8.0):
        req = {
            "type": "PAUSE",
            "audioSource": url,
            "trackTimeSeconds": pos,
        }
        if exec_ms is not None:
            req["executeAtServerTime"] = int(exec_ms)
        return self.send_command(req, timeout)

    def _negotiate_execat(self, exec_ms, echo):
        """The echo's serverTimeToExecute reveals whether the build honored
        our absolute execute time. Honored once => use it from then on;
        rejected => fall back to server-chosen instants for this session."""
        if exec_ms is None:
            return
        got = echo.get("serverTimeToExecute")
        if not isinstance(got, (int, float)):
            return
        agreed = abs(got - exec_ms) <= 60
        if self.execat_supported is None:
            self.execat_supported = agreed
            LOG.info("executeAtServerTime %s by server",
                     "honored" if agreed else "ignored — falling back")

    # -- HTTP helpers ----------------------------------------------------

    def _http(self, method, url, body=None, headers=None, timeout=15):
        return http_request(method, url, body, headers, timeout)

    def register_source(self, media_name, public_url):
        """POST /upload/complete — binds the URL to the room and makes devices
        preload it (SET_AUDIO_SOURCES)."""
        body = json.dumps(
            {
                "roomId": self.cfg["roomId"],
                "originalName": media_name,
                "publicUrl": public_url,
            }
        ).encode("utf-8")
        status, resp = self._http(
            "POST",
            self.http_base + "/upload/complete",
            body=body,
            headers={
                "Content-Type": "application/json",
                # The endpoint itself is unauthenticated inside beatsync —
                # Caddy gates it on the shared FALCONBEAT_TOKEN.
                "Authorization": "Bearer %s" % self.cfg["falconbeatToken"],
            },
        )
        if status >= 300:
            raise RuntimeError(
                "upload/complete -> HTTP %d: %s" % (status, resp[:200])
            )

    def push_file(self, local_path, stored_name):
        """PUT file to ingest -> returns public URL devices will fetch.
        Streams from disk (Content-Length set) so upload memory doesn't
        scale with file size."""
        ingest = self.cfg["httpBase"]
        quoted = urllib.parse.quote(stored_name)
        f = open(local_path, "rb")
        try:
            status, resp = self._http(
                "PUT",
                "%s/media/%s" % (ingest, quoted),
                body=f,
                headers={
                    "Authorization": "Bearer %s" % self.cfg["falconbeatToken"],
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(os.path.getsize(local_path)),
                },
                timeout=120,
            )
        finally:
            f.close()
        if status >= 300:
            raise RuntimeError("ingest PUT -> HTTP %d: %s" % (status, resp[:200]))
        stored = stored_name
        try:
            stored = json.loads(resp).get("stored", stored_name)
        except ValueError:
            pass
        # Path-only URL: the listener resolves "/media/..." against the
        # page origin, so devices always fetch through the same ingress
        # they arrived on — no public-base setting needed.
        return "/media/%s" % urllib.parse.quote(stored)


# --------------------------------------------------------------------------
# FPP helpers
# --------------------------------------------------------------------------


def find_media_file(name):
    """Resolve a playlist media name to (path, subdir) under music/ or
    videos/ — or (None, None).

    The daemon runs as root and uploads whatever this returns, so only a
    plain filename whose real path stays inside music/ or videos/ resolves:
    "../" traversal, subdirectories, symlink escapes, and loose files
    directly under media/ (settings, secrets) are all refused."""
    if not name or os.path.basename(name) != name:
        return None, None
    for sub in ("music", "videos"):
        base = os.path.realpath(os.path.join(MEDIADIR, sub))
        cand = os.path.realpath(os.path.join(base, name))
        if cand.startswith(base + os.sep) and os.path.isfile(cand):
            return cand, sub
    return None, None


# -- video -> audio extraction ---------------------------------------------
# Playlist "video" entries carry the soundtrack visitors should hear, but
# pushing the whole video costs daemon memory, ingest disk, and a per-device
# download. Instead ffmpeg lifts the audio track once into AUDIO_DIR,
# keyed on source path+size+mtime so an edited video re-converts. Stream
# copy keeps timing bit-identical; a transcode adds ~25-50ms codec delay
# (absorbed by the sync offset).

BROWSER_SAFE_AUDIO = {"aac": ".m4a", "mp3": ".mp3"}


def _audio_codec(path):
    """Primary audio codec of a media file via ffprobe, or None."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                path,
            ],
            capture_output=True, timeout=30,
        )
        first = out.stdout.decode("utf-8", "replace").strip().split(",")[0]
        return first.strip() or None
    except Exception:
        return None


def _audio_cache_path(src_path, ext):
    st = os.stat(src_path)
    key = "%s|%d|%d" % (src_path, st.st_size, int(st.st_mtime))
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    base = re.sub(
        r"[^A-Za-z0-9_.-]+", "_",
        os.path.splitext(os.path.basename(src_path))[0],
    )[:60]
    return os.path.join(AUDIO_DIR, "%s.%s%s" % (base or "audio", h, ext))


def fpp_status():
    """Full status dict from the FPP status API, or None if unreachable."""
    try:
        with urllib.request.urlopen(FPP_STATUS_URL, timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def fpp_system_info():
    """FPP system info ({HostName, HostDescription, ...}), or None."""
    try:
        with urllib.request.urlopen(FPP_SYSINFO_URL, timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def fpp_position_seconds():
    """Current media position from the FPP status API. Returns (seconds, playing)."""
    st = fpp_status() or {}
    playing = st.get("status_name") == "playing" or st.get("status") == 1
    ms = st.get("milliseconds_elapsed")
    if ms is not None:
        try:
            return float(ms) / 1000.0, bool(playing)
        except (TypeError, ValueError):
            pass
    # Fallback: seconds_played is integer-truncated (0-1s low) — only
    # reached on FPP builds without milliseconds_elapsed.
    raw = st.get("seconds_played", 0)
    try:
        pos = float(raw)
    except (TypeError, ValueError):
        # defensive: some FPP builds format it "MM:SS"
        try:
            parts = str(raw).split(":")
            pos = float(parts[-1]) + 60 * float(parts[-2])
        except Exception:
            pos = 0.0
    return pos, bool(playing)


def fpp_media_duration_seconds(name):
    """Precise media duration (seconds) from the FPP media API, or None.
    The endpoint nests the record under the requested name:
    {"song.mp3": {"duration": 186.29}}."""
    url = "http://127.0.0.1/api/media/%s/duration" % urllib.parse.quote(str(name))
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            resp = json.loads(r.read())
        rec = resp.get(name)
        if not isinstance(rec, dict):
            rec = resp  # tolerate a flat {"duration": ...} variant
        dur = float(rec.get("duration"))
        return dur if dur > 0 else None
    except Exception:
        return None


def fpp_sequence_duration_seconds(name):
    """fseq duration (StepTime * NumFrames) via the sequence meta API, or None."""
    url = "http://127.0.0.1/api/sequence/%s/meta" % urllib.parse.quote(str(name))
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            meta = json.loads(r.read())
        dur = float(meta.get("StepTime") or 0) * \
            float(meta.get("NumFrames") or 0) / 1000.0
        return dur if dur > 0 else None
    except Exception:
        return None


def playlist_media_files(playlist):
    """Ordered media filenames referenced by a playlist JSON (any section).
    Repeats are kept — lookahead needs real entry order to find "next"."""
    names = []
    for section in ("leadIn", "mainPlaylist", "leadOut"):
        for entry in playlist.get(section) or []:
            name = entry_media_name(entry)
            if name:
                names.append(name)
    return names


def entry_media_name(entry):
    """Media filename from a single playlist entry dict (schema varies).

    FPP playlist files use "mediaName" (extraMediaName/extraMedia hold
    alternates we don't push). The runtime currentEntry the callback
    carries may instead nest {"media": {"mediaFilename": ...}}."""
    if not isinstance(entry, dict):
        return None
    media = entry.get("media")
    if isinstance(media, dict) and media.get("mediaFilename"):
        return media.get("mediaFilename")
    return (entry.get("mediaFilename") or entry.get("mediaName")
            or entry.get("Media"))


def fpp_playlist_files(name):
    """Ordered media filenames for a named playlist via the FPP API.

    The playlist callback only carries Playlist::GetInfo() — name, size,
    currentEntry, Action/Section/Item — never the entry list itself, so
    the queue always comes from this fetch. mergeSubs expands sub-playlist
    entries (type "playlist" carries no media of its own)."""
    if not name:
        return []
    try:
        base = FPP_STATUS_URL.rsplit("/api/", 1)[0]
        url = ("%s/api/playlist/%s?mergeSubs=1"
               % (base, urllib.parse.quote(str(name))))
        with urllib.request.urlopen(url, timeout=3) as r:
            return playlist_media_files(json.loads(r.read()))
    except Exception as e:
        LOG.warning("playlist %s fetch failed: %s", name, e)
        return []


def playlist_entries(playlist):
    """Ordered (type, media_name) per playlist entry. Non-audio entries
    keep their type slot — the prescheduler must see pause/sequence-only
    entries between songs, not just the media names."""
    out = []
    for section in ("leadIn", "mainPlaylist", "leadOut"):
        for entry in playlist.get(section) or []:
            out.append((str(entry.get("type") or "").lower(),
                        entry_media_name(entry)))
    return out


def fpp_playlist_json(name):
    """Raw playlist JSON for a named playlist via the FPP API."""
    if not name:
        return None
    try:
        base = FPP_STATUS_URL.rsplit("/api/", 1)[0]
        url = ("%s/api/playlist/%s?mergeSubs=1"
               % (base, urllib.parse.quote(str(name))))
        with urllib.request.urlopen(url, timeout=3) as r:
            return json.loads(r.read())
    except Exception as e:
        LOG.warning("playlist %s fetch failed: %s", name, e)
        return None


def fpp_playlist_entries(name):
    pl = fpp_playlist_json(name)
    return playlist_entries(pl) if pl else []


# --------------------------------------------------------------------------
# Daemon
# --------------------------------------------------------------------------


class FalconBeatDaemon:
    def __init__(self):
        self.cfg = load_settings()
        self.session = None
        self._session_lock = threading.Lock()  # guards self.session reference
        self._connect_lock = threading.Lock()  # serializes connect+re-register
        self._pushed = {}        # media_name -> public_url (uploaded, room-live)
        self._registered = set()  # urls registered on the room this connection
        self._pending_file_dels = set()  # names whose ingest DELETE failed
        self._current_source = None
        self._current_media = None
        self._played_urls = set()  # sources that finished — pruned on next PLAY
        self._queue = []           # ordered playlist media names (dupes kept)
        self._queue_idx = -1       # queue position of the current track
        self._entries = []         # full (type, media_name) playlist sequence
        self._entry_of_media = []  # media-queue idx -> index into _entries
        self._wrap_next = False    # playlist repeats — last entry's "next" is 0
        self._queue_fetch_fail = {}  # name -> monotonic ts of last failed fetch
        self._playlist_active = False
        self._disconnect_timer = None
        self._shutdown = threading.Event()
        self._push_lock = threading.Lock()
        self._reg_lock = threading.Lock()  # serialize register_source checks
        self._pushing = {}  # media_name -> threading.Event (set when done)
        self._push_errors = {}  # media_name -> last push failure (for waiters)
        self._state_lock = threading.Lock()
        self._np_doc = None  # last nowplaying doc pushed to ingest
        self._show_pushed = None  # last show config doc pushed (dedupe)
        self._sysinfo = None    # cached FPP system info (None = retry)
        self._failed_media = {}  # filename -> epoch of last failed lookup
        self._pb_lock = threading.Lock()  # serialize play()/pause()
        self._presched = None   # {media, url, t_local} pending pre-schedule
        self._dur_cache = {}    # media name -> duration seconds (API-cached)
        self._seq_dur_cache = {}  # sequence name -> duration seconds
        # Learned entry-boundary -> next-audio-start delay. fppd ends an
        # entry at min(media, seq) length, then the next entry's
        # OpenMediaOutput + output-pipeline start run before audio is
        # audible. The verify pass converges this to the real value.
        self._extra_gap = self.PREP_DELAY_S
        self._tick = threading.Event()  # wake reconciler onto fast cadence
        self._last_play = None  # last PLAY sent, for the verify pass
        self._outages = set()   # log-once keys for down-server warnings
        self._state_sig = None  # last status.json content signature
        self._state_written = 0.0

    # -- session lifecycle ----------------------------------------------

    def _on_ws_event(self, msg):
        if msg.get("type") == "_disconnected":
            LOG.info("FalconBeat session dropped")
            self._registered.clear()
            if self._playlist_active:
                self._schedule_reconnect()

    def _schedule_reconnect(self):
        def retry():
            delay = 2.0
            while self._playlist_active and not self._shutdown.is_set():
                try:
                    self._connect()
                    self._recovered("ws", "FalconBeat session recovered")
                    return
                except Exception as e:
                    self._outage(
                        "ws", "FalconBeat unreachable: %s" % e)
                    self._shutdown.wait(delay)
                    delay = min(delay * 2, 30.0)

        threading.Thread(target=retry, daemon=True).start()

    def _connect(self):
        # Held across the (potentially slow) WS handshake + registration, but
        # NOT _session_lock — pause()/drop() must stay able to read the
        # session reference while a connect is in flight.
        with self._connect_lock:
            with self._session_lock:
                if self.session is None:
                    self.cfg = load_settings()
                    self.session = FalconBeatSession(self.cfg, self._on_ws_event)
                session = self.session
            if not session._connected.is_set():
                session.connect()  # retry if the last attempt failed
            # Server-side restarts forget room->source bindings; re-register
            # the live window only (current + next). Anything else in
            # _pushed is stale — re-registering it re-adds evicted tracks to
            # the room, so evict it instead.
            keep = set()
            if self._current_source:
                keep.add(self._current_source)
            nxt_i = self._queue_next_idx()
            if nxt_i >= 0:
                nxt_url = self._pushed.get(self._queue[nxt_i])
                if nxt_url:
                    keep.add(nxt_url)
            stale = []
            with self._reg_lock:
                for name, url in list(self._pushed.items()):
                    if url not in keep:
                        stale.append(url)
                        continue
                    if url in self._registered:
                        continue
                    try:
                        session.register_source(name, url)
                        self._registered.add(url)
                    except Exception as e:
                        LOG.warning("re-register %s failed: %s", name, e)
            if stale:
                self._delete_room_sources(stale, reason="stale after reconnect")
            return session

    def _disconnect_later(self):
        grace = float(self.cfg.get("disconnectGraceSec", "5") or 5)

        def drop():
            with self._session_lock:
                if self.session:
                    LOG.info("dropping FalconBeat session")
                    self.session.close()
                    self.session = None
                self._registered.clear()

        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        self._disconnect_timer = threading.Timer(grace, drop)
        self._disconnect_timer.daemon = True
        self._disconnect_timer.start()

    # -- now-playing metadata (ingest /config/nowplaying) ------------------
    # Contract: plugin PUTs {playing, title?, artist?, album?, filename,
    # updatedAtMs} per media event and {playing:false} on stop. The listener
    # page treats updatedAtMs stale >90s as off air, so we heartbeat every
    # 30s while a track is playing.

    NOWPLAYING_HEARTBEAT_S = 30.0

    def _put_config(self, name, doc):
        ingest = self.cfg["httpBase"]
        status, resp = http_request(
            "PUT",
            "%s/config/%s" % (ingest, name),
            body=json.dumps(doc).encode("utf-8"),
            headers={
                "Authorization": "Bearer %s" % self.cfg["falconbeatToken"],
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        if status >= 300:
            raise RuntimeError(
                "PUT /config/%s -> HTTP %d: %s" % (name, status, resp[:200])
            )

    def _outage(self, key, msg):
        """Log a failure once per outage — a down server would otherwise
        warn every retry until it comes back."""
        if key not in self._outages:
            self._outages.add(key)
            LOG.warning("%s (further failures silent until recovery)", msg)

    def _recovered(self, key, msg):
        if key in self._outages:
            self._outages.discard(key)
            LOG.info(msg)

    def _set_nowplaying(self, doc):
        self._np_doc = doc
        try:
            self._put_config("nowplaying", doc)
            self._recovered("nowplaying", "nowplaying push recovered")
        except Exception as e:
            self._outage("nowplaying",
                         "nowplaying update failed: %s" % e)

    def _nowplaying_heartbeat(self):
        while not self._shutdown.wait(self.NOWPLAYING_HEARTBEAT_S):
            doc = self._np_doc
            if not doc or not doc.get("playing"):
                continue
            if not configured(load_settings()):
                continue
            doc["updatedAtMs"] = epoch_ms()
            try:
                self._put_config("nowplaying", doc)
                self._recovered("nowplaying", "nowplaying push recovered")
            except Exception as e:
                self._outage("nowplaying",
                             "nowplaying heartbeat failed: %s" % e)

    # -- show info (ingest /config/show) ---------------------------------
    # FPP is the single source of truth: the daemon renders the show doc
    # from plugin settings (falling back to FPP HostName/HostDescription)
    # and pushes it whenever the rendered doc changes — settings saves land
    # on the listener page within one reconcile tick.

    def _fpp_sysinfo(self):
        if self._sysinfo is None:
            self._sysinfo = fpp_system_info()  # stays None on failure -> retry
        return self._sysinfo or {}

    def _show_doc(self):
        cfg = self.cfg
        sysinfo = self._fpp_sysinfo()
        doc = {
            "showName": (cfg.get("showName") or "").strip()
            or (sysinfo.get("HostName") or "").strip()
            or "FalconBeat",
            "links": [],
        }
        tagline = (cfg.get("showTagline") or "").strip() or (
            sysinfo.get("HostDescription") or ""
        ).strip()
        if tagline:
            doc["tagline"] = tagline
        for field, key in (
            ("hostedBy", "showHostedBy"),
            ("description", "showDescription"),
            ("schedule", "showSchedule"),
            ("location", "showLocation"),
            ("publicUrl", "showPublicUrl"),
        ):
            # The textarea rows (description, links) save with newlines
            # escaped as literal \n — plugin.* is one setting per line.
            v = (cfg.get(key) or "").replace("\\n", "\n").strip()
            if v:
                doc[field] = v
        # Links are typed "Label = URL", one per line. Split on the first
        # '=' so URLs may carry query strings; blanks/bad lines are dropped.
        for line in (
            cfg.get("showLinks") or ""
        ).replace("\\n", "\n").splitlines():
            line = line.strip()
            if not line:
                continue
            label, sep, url = line.partition("=")
            if sep and url.strip():
                doc["links"].append({"label": label.strip(), "url": url.strip()})
        return doc

    def _push_show(self):
        doc = self._show_doc()
        if doc == self._show_pushed:
            return
        try:
            self._put_config("show", doc)
            self._show_pushed = doc
            self._recovered("show", "show config push recovered")
            LOG.info("show config pushed")
        except Exception as e:
            self._outage("show", "show config push failed: %s" % e)

    # -- pre-scheduled next-track PLAY ------------------------------------
    # While the current song winds down we broadcast the next track's PLAY
    # with an absolute executeAt — devices arm a future start INSTEAD of
    # racing the transition. The media callback then refines the instant
    # (supersede is free client-side). This is insurance: it only matters
    # when the callback arrives late or the device set is slow to ACK.

    def _entry_remaining_seconds(self, st):
        """Precise seconds until the current entry ends, or None.

        fppd's seconds_remaining is (lengthMS - elapsedMS)/1000 int-
        truncated — up to ~1s low, which biases an armed start early.
        Reconstruct it exactly: milliseconds_elapsed is the media clock
        in ms and the entry length is min(media, sequence) duration —
        fppd's GetLengthInMS for "both" entries — both available via
        localhost APIs."""
        song = st.get("current_song")
        ms = st.get("milliseconds_elapsed")
        if not song or ms is None:
            return None
        try:
            elapsed = float(ms) / 1000.0
        except (TypeError, ValueError):
            return None
        dur = self._dur_cache.get(song)
        if dur is None:
            dur = fpp_media_duration_seconds(song)
            if dur:
                self._dur_cache[song] = dur
        if not dur:
            return None
        seq = st.get("current_sequence")
        if seq:
            sdur = self._seq_dur_cache.get(seq)
            if sdur is None:
                sdur = fpp_sequence_duration_seconds(seq)
                if sdur:
                    self._seq_dur_cache[seq] = sdur
            if sdur and sdur < dur:
                dur = sdur
        return dur - elapsed

    def _maybe_preschedule(self, st):
        if self._presched is not None:
            return
        rem = self._entry_remaining_seconds(st)
        if rem is None:
            try:
                rem = float(st.get("seconds_remaining") or 0)
            except (TypeError, ValueError):
                return
        if not (0 < rem <= self.PRE_SCHEDULE_AHEAD_S):
            return
        nxt_i = self._queue_next_idx()
        if nxt_i < 0:
            return
        if self._entries:
            # Only arm when the immediately following playlist ENTRY is
            # that media item. A pause, sequence-only, or event entry in
            # between is a real gap — an armed PLAY would fire into it
            # long before fppd reached the track. The media callbacks
            # for those entries park listeners instead; their event
            # path sends PLAY when the track actually starts.
            fi = (self._entry_of_media[self._queue_idx]
                  if 0 <= self._queue_idx < len(self._entry_of_media)
                  else -1)
            fj = self._entry_of_media[nxt_i]
            adjacent = (fj == fi + 1)
            wrapped = (self._wrap_next and fi >= 0
                       and fi == len(self._entries) - 1 and fj == 0)
            if not (adjacent or wrapped):
                return
        name = self._queue[nxt_i]
        if name not in self._pushed:
            return  # lookahead hasn't delivered it — the event path copes
        off = float(self.cfg.get("syncOffsetMs") or 0) / 1000.0
        try:
            url = self.ensure_source(name)
            session = self._connect()
            # T1 = predicted entry end + learned boundary->audio gap + a
            # small buffer. The armed node starts at T1 already pos_at
            # seconds into the track, so its position-0 lands at
            # (entry end + _extra_gap) — the instant the next track's
            # audio actually starts, not the silent entry boundary
            # itself (fppd spends _extra_gap on the next entry's
            # OpenMediaOutput + output-pipeline start).
            t1_local = (time.time() + rem + self._extra_gap
                        + self.SCHEDULE_GAP_S + off)
            pos_at = self.SCHEDULE_GAP_S + off
            session.send_play(
                url, max(0.0, pos_at),
                session.server_now_ms()
                + (t1_local - time.time()) * 1000.0)
            self._presched = {
                "media": name, "url": url, "t_local": t1_local,
            }
            LOG.info("pre-scheduled %s (fires in %.1fs)",
                     name, t1_local - time.time())
        except Exception as e:
            LOG.warning("pre-schedule %s failed: %s", name, e)

    # -- fppd status reconciler ------------------------------------------
    # The callbacks only fire on NEW transitions: a playlist already playing
    # when the daemon comes up (fppd restarted, schedule resumed mid-show,
    # a callback missed entirely) would never reach us. Poll fppd every few
    # seconds and converge — the event handlers dedupe on _current_media so
    # this never double-fires PLAY.

    RECONCILE_PERIOD_S = 3.0
    IDLE_PERIOD_S = 30.0  # callbacks announce transitions; this is a net
    FAILED_RETRY_S = 60.0
    # Pre-scheduling (option C): while a track plays, send the next one's
    # PLAY early with an absolute executeAt so devices hold a future start
    # instead of racing the transition. The media event then refines it.
    # The window must exceed the server's 3s audio-load timeout.
    PRE_SCHEDULE_AHEAD_S = 4.5
    # Estimated media-end → next-audio gap for the insurance schedule only.
    SCHEDULE_GAP_S = 0.4
    # Estimated fppd PreparePlay → audio-start latency anchoring T2.
    PREP_DELAY_S = 0.45
    VERIFY_DELAY_S = 0.9
    VERIFY_ERR_S = 0.30

    def _reconcile_loop(self):
        while not self._shutdown.is_set():
            # Idle ticks only keep an eye out for a missed playlist start —
            # events drive everything while a show is actually running.
            period = (self.RECONCILE_PERIOD_S if self._playlist_active
                      else self.IDLE_PERIOD_S)
            self._tick.wait(period)  # _play kicks us onto the fast cadence
            self._tick.clear()
            if self._shutdown.is_set():
                break
            try:
                self.cfg = load_settings()
                if not configured(self.cfg):
                    # Disabled or unconfigured: no network, no fppd polls,
                    # and no lingering admin session holding the room.
                    self._playlist_active = False
                    with self._session_lock:
                        if self.session is not None:
                            LOG.info("unconfigured/disabled — dropping session")
                            self.session.close()
                            self.session = None
                    self._registered.clear()
                    continue
                self._push_show()
                st = fpp_status()
                if not st:
                    continue
                status_name = str(st.get("status_name", "")).lower()
                song = st.get("current_song") or ""
                if song in ("0", "none"):
                    song = ""

                if self._presched and \
                        time.time() > self._presched["t_local"] + 8:
                    self._presched = None
                if status_name in ("playing", "paused"):
                    if not self._playlist_active:
                        LOG.info("fppd %s — reconciling into active state",
                                 status_name)
                        self._playlist_active = True
                        if self._disconnect_timer:
                            self._disconnect_timer.cancel()
                            self._disconnect_timer = None
                    if status_name == "playing" and song and \
                            song != self._current_media:
                        if not self._queue:
                            # Attached mid-playlist (missed the start event)
                            pl = st.get("current_playlist") or ""
                            if isinstance(pl, dict):
                                pl = pl.get("playlist") or ""
                            self._fetch_queue(pl)
                            self._queue_idx = -1
                        failed_at = self._failed_media.get(song)
                        if failed_at and epoch_ms() - failed_at < \
                                self.FAILED_RETRY_S * 1000:
                            continue
                        try:
                            self.play(song)
                            self._failed_media.pop(song, None)
                            self._recovered("play", "playback recovered")
                        except Exception as e:
                            self._outage(
                                "play", "reconcile PLAY %s failed: %s"
                                % (song, e))
                            self._failed_media[song] = epoch_ms()
                    elif status_name == "paused" and self._current_source:
                        self.pause()
                    elif status_name == "playing" and not song \
                            and self._current_source:
                        # sequence-only entry on air — park listeners
                        self.pause()
                    if status_name == "playing" and song:
                        self._maybe_preschedule(st)
                elif status_name in ("idle", "stopping"):
                    if self._playlist_active or self._current_source:
                        LOG.info("fppd %s — reconciling to stopped",
                                 status_name)
                        self._playlist_active = False
                        self._set_queue([])
                        self._queue_idx = -1
                        self._wrap_next = False
                        self._queue_fetch_fail.clear()
                        self.pause()
                        if self._np_doc and self._np_doc.get("playing"):
                            self._set_nowplaying(
                                {"playing": False, "updatedAtMs": epoch_ms()}
                            )
                        self._delete_room_sources(
                            list(self._registered), reason="show stopped")
                        self._disconnect_later()
            except Exception as e:
                LOG.warning("reconcile error: %s", e)

    # -- push/register/play ----------------------------------------------

    def _video_audio(self, path):
        """Uploadable audio path for a video entry: extract the soundtrack
        into the plugindata cache (keyed on path+size+mtime). Browser-safe
        codecs stream-copy (cheap, bit-identical timing); anything else
        transcodes to AAC under nice — refused while a show is running on a
        single-core board, where it could starve fppd."""
        codec = _audio_codec(path)
        ext = BROWSER_SAFE_AUDIO.get(codec, ".m4a")
        out = _audio_cache_path(path, ext)
        if os.path.isfile(out):
            return out
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError(
                "ffmpeg not found — cannot extract %s" % path)
        os.makedirs(AUDIO_DIR, exist_ok=True)
        # ffmpeg infers the container from the extension, so the temp name
        # must keep it LAST: name.part.m4a, not name.m4a.part.
        tmp = "%s.part%s" % (out[: -len(ext)], ext)
        nice = [p for p in (shutil.which("nice"),) if p]
        if codec in BROWSER_SAFE_AUDIO:
            # -readrate keeps ffmpeg from racing the SD card while fppd is
            # reading the same show files.
            cmd = nice + ["-n", "19"] * bool(nice) + [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-readrate", "3", "-i", path, "-vn", "-c:a", "copy", tmp,
            ]
        else:
            if (os.cpu_count() or 1) < 2 and self._playlist_active:
                raise RuntimeError(
                    "video %s: codec %r needs a re-encode — refused while a "
                    "show is playing on a single-core board"
                    % (os.path.basename(path), codec))
            cmd = nice + ["-n", "19"] * bool(nice) + [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-readrate", "2", "-i", path, "-vn", "-threads", "1",
                "-c:a", "aac", "-b:a", "160k", tmp,
            ]
        try:
            subprocess.run(
                cmd, check=True, timeout=1800,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            os.replace(tmp, out)
            LOG.info("extracted audio from video %s -> %s",
                     os.path.basename(path), os.path.basename(out))
            # Cache names are content-keyed, so an edited video orphans its
            # old extraction — prune entries not touched in a week.
            cutoff = time.time() - 7 * 86400
            try:
                for fn in os.listdir(AUDIO_DIR):
                    fp = os.path.join(AUDIO_DIR, fn)
                    if os.path.getmtime(fp) < cutoff:
                        os.unlink(fp)
            except OSError:
                pass
            return out
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or b"").decode("utf-8", "replace")[-300:]
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise RuntimeError(
                "audio extraction failed for %s: rc=%d %s"
                % (path, e.returncode, detail))
        except Exception as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise RuntimeError(
                "audio extraction failed for %s: %s" % (path, e))

    def ensure_source(self, media_name):
        """Make sure media_name is uploaded to ingest AND registered on the
        room. Returns the public audioSource URL."""
        url = self._pushed.get(media_name)
        if url and url in self._registered:
            return url

        if url is None:
            # Per-file in-flight marker so the pre-push thread and a media
            # event can't double-push the same file.
            with self._push_lock:
                done = self._pushing.get(media_name)
                if done is None:
                    done = threading.Event()
                    self._pushing[media_name] = done
                    owner = True
                else:
                    owner = False
            if not owner:
                if not done.wait(120):
                    raise TimeoutError(
                        "push of %s still in flight after 120s" % media_name
                    )
                url = self._pushed.get(media_name)
                if url is None:
                    raise RuntimeError(
                        "push of %s failed in another thread: %s"
                        % (media_name,
                           self._push_errors.get(media_name, "unknown"))
                    )
            else:
                try:
                    path, sub = find_media_file(media_name)
                    if not path:
                        raise FileNotFoundError(
                            "media file %r not found under music/ or "
                            "videos/ (or path refused)" % media_name
                        )
                    upload = self._video_audio(path) if sub == "videos" \
                        else path
                    LOG.info("pushing %s -> ingest", media_name)
                    url = self._connect().push_file(
                        upload, os.path.basename(upload)
                    )
                    self._pushed[media_name] = url
                    self._push_errors.pop(media_name, None)
                    LOG.info("pushed: %s", url)
                except Exception as e:
                    self._push_errors[media_name] = e
                    raise
                finally:
                    done.set()
                    # Drop the marker: waiters already hold the Event, and a
                    # failed push must not permanently blacklist the file —
                    # the next caller retries as owner.
                    with self._push_lock:
                        self._pushing.pop(media_name, None)

        url = self._pushed[media_name]
        if url not in self._registered:
            # NB: take _connect() outside _reg_lock — _connect's re-register
            # loop holds _connect_lock then wants _reg_lock; calling it here
            # under _reg_lock would invert the order and can deadlock.
            session = self._connect()
            with self._reg_lock:
                if url not in self._registered:
                    session.register_source(media_name, url)
                    self._registered.add(url)
        return url

    def play(self, media_name, data=None, event_ts=None):
        with self._pb_lock:
            self._play(media_name, data, event_ts)

    def _play(self, media_name, data=None, event_ts=None):
        if media_name == self._current_media:
            return
        self._playlist_active = True
        self._tick.set()  # reconcile may be parked in an idle wait
        presched = self._presched
        self._presched = None  # any real event supersedes a pending schedule
        # Push now-playing first — the page can show the track even while
        # devices are still loading the audio.
        doc = {
            "playing": True,
            "filename": media_name,
            "updatedAtMs": epoch_ms(),
        }
        for tag in ("title", "artist", "album"):
            v = (data or {}).get(tag)
            if v:
                doc[tag] = v
        self._set_nowplaying(doc)

        session = self._connect()
        url = self.ensure_source(media_name)
        pos, _playing = fpp_position_seconds()
        t_sample = time.time()
        t_ev = event_ts or t_sample
        off = float(self.cfg.get("syncOffsetMs") or 0) / 1000.0
        delay = session.expected_delay_s()
        # T2 = the instant devices start. When a pre-schedule is armed for
        # this media its instant stands — see below. Otherwise, with
        # executeAtServerTime honored we anchor to the media callback:
        # fppd fires it inside PreparePlay, ~PREP_DELAY_S before audio
        # actually starts, so T2 lands just after the track begins. The
        # position sent is always extrapolated to T2, so residual error is
        # only the status read's staleness — systematic, tunable with
        # Sync Offset. On unpatched servers T2 = now + the server's own
        # predicted delay, which behaves exactly as before.
        old_source = self._current_source
        # A pre-schedule armed for THIS media already carries the handoff —
        # its instant came from the duration-derived entry boundary plus
        # the learned gap, a better estimate than callback + PREP_DELAY.
        # PreparePlay can fire this callback well ahead of the real
        # transition, and anchoring T2 to it cuts the outgoing tail early
        # and makes _verify_start rewind the head audibly. Keep the armed
        # PLAY instead.
        armed = presched and presched["media"] == media_name and \
            presched["t_local"] > t_sample + 0.05
        if armed:
            t2_local = presched["t_local"]
            # Matches the pos_at the armed PLAY actually carried.
            target = max(0.0, self.SCHEDULE_GAP_S + off)
            LOG.info("PLAY %s: pre-scheduled start stands (fires in %.2fs)",
                     media_name, t2_local - t_sample)
            self._current_source = url
            self._current_media = media_name
            self._last_play = {
                "media": media_name, "url": url, "t2": t2_local,
                "target": target, "off": off, "armed": True,
            }
        else:
            probe = 0.077 if session.execat_supported is None else 0.0
            if session.execat_supported is not False:
                t2_local = max(t_ev + self.PREP_DELAY_S + off + probe,
                               t_sample + 0.05)
            else:
                t2_local = t_sample + delay + off
            target = pos + (t2_local - t_sample)
            exec_at = session.server_now_ms() + (t2_local - time.time()) * 1000.0
            LOG.info("PLAY %s @ %.2fs (pos %.2f -> T+%.2f%s, offset %.3f)",
                     media_name, target, pos, t2_local - t_ev,
                     "" if session.execat_supported is not False else
                     " server-picked", off)
            # A still-pending pre-schedule gets killed outright — the client
            # would supersede it at T2 anyway, but for a mid-list skip T2 can
            # land after its armed start and leave an audible blip.
            if presched and presched["url"] != url and \
                    presched["t_local"] > time.time():
                try:
                    session.send_pause(
                        presched["url"], 0.0,
                        session.server_now_ms() + delay * 1000.0)
                except Exception as e:
                    LOG.warning("cancel of pre-scheduled PLAY failed: %s", e)
            # Cut the previous track at the same instant the new one starts.
            if old_source and old_source != url:
                try:
                    session.send_pause(old_source, 0.0, exec_at)
                except Exception as e:
                    LOG.warning("pre-play PAUSE of old source failed: %s", e)
            echo = session.send_play(url, target, exec_at)
            self._current_source = url
            self._current_media = media_name
            self._last_play = {
                "media": media_name, "url": url, "t2": t2_local,
                "target": target, "off": off,
            }
        # Post-hoc check: ~0.9s later, compare where devices were told to
        # be against where fppd actually is; correct large misses once.
        vt = threading.Timer(
            self.VERIFY_DELAY_S, self._verify_start, args=(media_name,))
        vt.daemon = True
        vt.start()
        # Advance the playlist cursor: first matching entry strictly after
        # the previous position (handles repeats and mid-list skips). If it
        # only exists behind us the playlist looped — take the first match.
        for i in range(max(0, self._queue_idx + 1), len(self._queue)):
            if self._queue[i] == media_name:
                self._queue_idx = i
                break
        else:
            if media_name in self._queue:
                self._queue_idx = self._queue.index(media_name)
        # Upload+register the next entry while this one plays.
        if self._queue:
            threading.Thread(
                target=self._push_ahead, args=(self._queue_idx,), daemon=True
            ).start()
        # Keep only {current, next} in the room — late joiners preload just
        # the lookahead window, and skipped entries (cursor jumped past
        # them) get evicted along with played ones.
        keep_names = {media_name}
        nxt_i = self._queue_next_idx()
        if nxt_i >= 0:
            keep_names.add(self._queue[nxt_i])
            # Cursor only just advanced — add "up next" to the now-playing
            # doc (it was pushed early, before the cursor moved).
            doc["next"] = self._queue[nxt_i]
            self._set_nowplaying(doc)
        keep_urls = {
            u for n, u in self._pushed.items()
            if n in keep_names
        }
        if old_source and old_source != url:
            self._played_urls.add(old_source)
        stale = self._played_urls - keep_urls
        stale |= {
            u for n, u in self._pushed.items()
            if u in self._registered and u not in keep_urls
        }
        self._played_urls = set()
        if stale:
            self._delete_room_sources(sorted(stale), reason="played/skipped")
        if not armed:
            LOG.info("PLAY confirmed, executes at server ts %s",
                     echo.get("serverTimeToExecute"))

    # -- start-time verification -------------------------------------------
    # The PLAY is sent with an extrapolated position; ~0.9s later we check
    # where devices were told to be vs where fppd actually is. Error =
    # status-read staleness + anchor bias — a large miss means our estimate
    # was off enough to hear, so send ONE corrective pos-anchored PLAY and
    # log a Sync Offset hint.

    def _verify_start(self, media_name):
        lp = self._last_play
        if not lp or lp["media"] != media_name:
            return
        if self._current_media != media_name:
            return
        with self._session_lock:
            session = self.session
        if session is None or not session._connected.is_set():
            return
        pos2, playing = fpp_position_seconds()
        now = time.time()
        if lp["t2"] > now:
            # A kept pre-scheduled start hasn't fired yet — nothing to
            # verify until it does; check again just after.
            delay = lp["t2"] - now + 0.5
            if delay <= 6.0:
                vt = threading.Timer(
                    delay, self._verify_start, args=(media_name,))
                vt.daemon = True
                vt.start()
            return
        # Where devices are, extrapolated: pos at T2 + elapsed since T2.
        device_pos = lp["target"] + (now - lp["t2"])
        err = device_pos - pos2  # >0: devices ahead of fppd
        if not playing:
            return
        if abs(err) <= self.VERIFY_ERR_S:
            LOG.info("start verified: devices %+dms vs fppd", int(err * 1000))
            return
        if lp.get("armed"):
            # Armed math cancels syncOffset: err = real entry-end ->
            # audio-start gap minus our estimate. Converge halfway so
            # subsequent pre-scheduled starts land closer.
            self._extra_gap = min(2.5, max(
                0.0, self._extra_gap + err * 0.5))
            LOG.info("boundary->audio gap estimate now %.2fs",
                     self._extra_gap)
        delay = session.expected_delay_s()
        target2 = pos2 + delay + lp["off"]
        try:
            session.send_play(
                lp["url"], target2,
                session.server_now_ms() + delay * 1000.0)
            LOG.warning(
                "start was off by %+dms — sent corrective PLAY "
                "(if every song drifts the same way, adjust Sync Offset "
                "by %+dms)", int(err * 1000), int(err * 1000))
        except Exception as e:
            LOG.warning("verify correction failed: %s", e)

    def pause(self):
        with self._pb_lock:
            self._pause()

    def _pause(self):
        presched = self._presched
        self._presched = None
        with self._session_lock:
            session = self.session
        if session is None:
            return
        if presched and presched["t_local"] > time.time():
            # A pre-scheduled PLAY is pending on devices — land a PAUSE
            # just before its start time so the pending node is silenced
            # instead of firing into the stop/gap.
            try:
                session.send_pause(
                    presched["url"], 0.0,
                    session.server_now_ms()
                    + (presched["t_local"] - time.time() - 0.05) * 1000.0)
            except Exception as e:
                LOG.warning("cancel of pre-scheduled PLAY failed: %s", e)
        if self._current_source is None:
            return
        pos, _playing = fpp_position_seconds()
        delay = session.expected_delay_s()
        target = pos + delay
        try:
            session.send_pause(
                self._current_source, max(0.0, target),
                session.server_now_ms() + delay * 1000.0)
            LOG.info("PAUSE %s @ %.2fs", self._current_source, target)
        except Exception as e:
            LOG.warning("PAUSE failed: %s", e)
        if self._current_source:
            self._played_urls.add(self._current_source)
        self._current_source = None
        self._current_media = None
        self._set_nowplaying({"playing": False, "updatedAtMs": epoch_ms()})

    # -- fppd events ------------------------------------------------------

    def handle_event(self, ev):
        cb_type = ev.get("cb_type")
        data = ev.get("data") or {}

        self.cfg = load_settings()
        if not configured(self.cfg):
            return True  # disabled or unconfigured — nothing to do

        try:
            if cb_type == "playlist":
                return self._on_playlist(data)
            elif cb_type == "media":
                return self._on_media(data, event_ts=ev.get("ts"))
            return True
        except Exception as e:
            LOG.exception("event %s failed: %s", cb_type, e)
            return False

    def _on_playlist(self, data):
        action = str(data.get("Action", "")).lower()
        # Every playlist callback carries Playlist::GetInfo() — refresh the
        # wrap flag so repeat toggles mid-show take effect. Matches core's
        # own test (Playlist.cpp): another pass exists iff repeat is on and
        # loopCount is 0 or hasn't been reached (m_loop counts finished
        # wraps, so pass k has loop=k-1 → wraps while k < loopCount).
        try:
            repeat = int(data.get("repeat") or 0)
            loop_cnt = int(data.get("loopCount") or 0)
            loop = int(data.get("loop") or 0)
        except (TypeError, ValueError):
            repeat = loop_cnt = loop = 0
        self._wrap_next = bool(repeat) and (loop_cnt == 0 or (loop + 1) < loop_cnt)
        if action == "start":
            LOG.info("playlist start")
            self._playlist_active = True
            self._tick.set()  # reconcile onto the fast cadence now
            self._queue_fetch_fail.clear()  # fresh playlist — retry the fetch
            if self._disconnect_timer:
                self._disconnect_timer.cancel()
                self._disconnect_timer = None
            self._connect()
            # The callback data is Playlist::GetInfo() — it carries the
            # playlist "name" but never the entry list, so the queue comes
            # from the playlist API (sections in data are kept as a
            # best-effort first try for other FPP variants).
            entries = playlist_entries(data)
            names = [n for _t, n in entries if n]
            if not names:
                pl = (data.get("name") or data.get("Playlist")
                      or data.get("playlistName"))
                if not pl:
                    st = fpp_status() or {}
                    pl = st.get("current_playlist") or ""
                    if isinstance(pl, dict):
                        pl = pl.get("playlist") or ""
                entries = fpp_playlist_entries(pl)
                names = [n for _t, n in entries if n]
            self._set_queue(entries)
            LOG.info("playlist queue: %d entries%s", len(names),
                     "" if names else " (keys: %s) — lookahead disabled"
                     % ",".join(sorted(data.keys())))
            if names:
                threading.Thread(
                    target=self._push_start_window, daemon=True
                ).start()
        elif action in ("playing", "query_next"):
            # informational — playing confirms the playlist is live,
            # query_next is FPP's next-item hook. Neither ends playback.
            LOG.info("playlist %s", action)
            self._playlist_active = True
            self._tick.set()
            if action == "query_next":
                # Fired when the current entry finishes, Item = its index
                # within Section (an int, not the entry dict). The queue is
                # the source of truth for "next" — fetch it lazily if the
                # start event missed us, then push queue[cursor+1] now
                # rather than waiting for the next media event.
                if not self._queue:
                    self._fetch_queue(data.get("name"))
                if self._queue:
                    threading.Thread(
                        target=self._push_ahead, args=(self._queue_idx,),
                        daemon=True
                    ).start()
        elif action in ("pause", "paused"):
            LOG.info("playlist paused")
            self.pause()
        elif action in ("stop", "stopping", "stopped"):
            LOG.info("playlist %s", action)
            self._playlist_active = False
            self._set_queue([])
            self._queue_idx = -1
            self._wrap_next = False
            self._queue_fetch_fail.clear()
            self.pause()
            if self._np_doc and self._np_doc.get("playing"):
                self._set_nowplaying(
                    {"playing": False, "updatedAtMs": epoch_ms()}
                )
            # Empty the queue — devices get SET_AUDIO_SOURCES=[] and have
            # nothing left to resume. Closest thing the protocol has to a
            # kick; with an empty queue the room is inert until next show.
            self._delete_room_sources(list(self._registered),
                                      reason="show stopped")
            self._disconnect_later()
        else:
            LOG.info("playlist %s (unhandled)", action or "unknown")
        return True

    def _delete_room_sources(self, urls, reason=""):
        """DELETE_AUDIO_SOURCES — drops queue entries and broadcasts
        SET_AUDIO_SOURCES so devices unload them. Fire-and-forget (the reply
        is a ROOM_EVENT, not a SCHEDULED_ACTION echo). Evicted files are
        also removed from the ingest media dir so the volume doesn't fill
        with played tracks."""
        urls = [u for u in urls if u]
        if not urls:
            return
        with self._session_lock:
            session = self.session
        live = session is not None and session._connected.is_set()
        if live:
            try:
                session._send({"type": "DELETE_AUDIO_SOURCES", "urls": urls})
                LOG.info("deleted %d room source(s)%s", len(urls),
                         " (%s)" % reason if reason else "")
            except Exception as e:
                # Room still lists the sources — keep the bookkeeping so the
                # next eviction or reconnect retries instead of orphaning.
                LOG.warning("DELETE_AUDIO_SOURCES failed: %s", e)
                return
        # Eviction reached the room (or there is no live room). Forget the
        # bindings atomically: _pushed.pop and _registered.discard under one
        # lock, so _connect()'s re-register loop can never observe a source
        # that is in _pushed but not _registered and resurrect it.
        urlset = set(urls)
        with self._reg_lock:
            names = [n for n, u in self._pushed.items() if u in urlset]
            for n in names:
                self._pushed.pop(n, None)
            self._registered.difference_update(urls)
        if names:
            # The stored filename comes from the URL, not the media name —
            # a video entry uploads as its extracted-audio file.
            self._delete_media_files(
                [urllib.parse.unquote(u.rsplit("/", 1)[-1]) for u in urls]
            )

    def _delete_media_files(self, names):
        """DELETE evicted files via ingest. Failures go to
        _pending_file_dels and are retried on the next call — a lingering
        file is harmless but shouldn't leak forever."""
        ingest = self.cfg["httpBase"]
        for name in self._pending_file_dels | set(names):
            try:
                quoted = urllib.parse.quote(os.path.basename(name))
                status, resp = http_request(
                    "DELETE",
                    "%s/media/%s" % (ingest, quoted),
                    headers={
                        "Authorization": "Bearer %s" % self.cfg["falconbeatToken"]
                    },
                    timeout=15,
                )
                if status < 300:
                    LOG.info("deleted media file %s", name)
                    self._pending_file_dels.discard(name)
                elif status == 404:
                    self._pending_file_dels.discard(name)
                else:
                    LOG.warning("ingest DELETE %s -> HTTP %d: %s",
                                name, status, resp[:200])
                    self._pending_file_dels.add(name)
            except Exception as e:
                LOG.warning("ingest DELETE %s failed: %s", name, e)
                self._pending_file_dels.add(name)

    def _fetch_queue(self, name):
        """Populate _queue from the playlist API with a 60s failure backoff.
        A sequence played directly has no file-backed playlist (404) — don't
        re-request it every reconcile tick."""
        if not name or self._queue:
            return
        if time.monotonic() - self._queue_fetch_fail.get(name, -61.0) < 60:
            return
        entries = fpp_playlist_entries(name)
        names = [n for _t, n in entries if n]
        if names:
            self._set_queue(entries)
            self._queue_fetch_fail.pop(name, None)
            LOG.info("playlist queue: %d entries (%s)", len(names), name)
        else:
            self._queue_fetch_fail[name] = time.monotonic()

    def _push_start_window(self):
        """Playlist start: entries 0 and 1 both belong in the room — the
        lookahead then keeps {current, next} topped up from there."""
        for i in range(2):
            if i >= len(self._queue):
                break
            if not self._playlist_active or self._shutdown.is_set():
                return
            try:
                self.ensure_source(self._queue[i])
            except Exception as e:
                LOG.warning("start push %s failed: %s", self._queue[i], e)

    def _set_queue(self, entries):
        """Store the full playlist entry sequence and derive the media-only
        queue + media→entry index map. Pause and sequence-only entries are
        kept in _entries so the prescheduler can refuse to arm a track
        across a gap it would fire into early."""
        self._entries = entries
        self._entry_of_media = [i for i, (_t, n) in enumerate(entries) if n]
        self._queue = [entries[i][1] for i in self._entry_of_media]

    def _queue_next_idx(self):
        """Queue position of the entry after the cursor. On a repeating
        playlist the last entry's "next" wraps to index 0."""
        if not self._queue:
            return -1
        i = self._queue_idx + 1
        if i < len(self._queue):
            return i
        return 0 if self._wrap_next else -1

    def _push_ahead(self, idx):
        """Ensure the playlist entry after queue position idx is uploaded +
        registered — devices then download it while the current track plays,
        so the next PLAY's device download isn't racing the schedule
        delay."""
        if not self._playlist_active or self._shutdown.is_set():
            return
        nxt = idx + 1
        if nxt >= len(self._queue):
            nxt = 0 if self._wrap_next else -1
        if 0 <= nxt < len(self._queue):
            try:
                self.ensure_source(self._queue[nxt])
            except Exception as e:
                LOG.warning("lookahead push %s failed: %s",
                            self._queue[nxt], e)

    def _on_media(self, data, event_ts=None):
        entry_type = str(data.get("type", "")).lower()
        media_name = data.get("Media") or ""
        if entry_type in ("both", "media", "video") and media_name:
            # fresh media -> any pending disconnect is moot while a playlist runs
            try:
                # The callback already returned — fppd is playing. Devices
                # get an absolute start instant anchored to the event, with
                # the position extrapolated to it.
                self.play(media_name, data, event_ts=event_ts)
                self._recovered("play", "playback recovered")
                return True
            except Exception as e:
                self._outage("play", "PLAY %s failed: %s" % (media_name, e))
                LOG.debug("PLAY %s traceback:", media_name, exc_info=True)
                return False
        elif entry_type in ("pause", "sequence", "event"):
            # non-audio playlist entry: park listeners. _pause also drops a
            # pending pre-schedule — it must see _presched, so don't clear
            # it here first.
            self.pause()
        return True

    # -- control socket ---------------------------------------------------

    def _serve_control(self, srv):
        while not self._shutdown.is_set():
            try:
                r, _, _ = select.select([srv], [], [], 1.0)
            except (OSError, ValueError, select.error):
                return
            if not r:
                continue
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            threading.Thread(
                target=self._handle_conn, args=(conn,), daemon=True
            ).start()

    def _handle_conn(self, conn):
        try:
            conn.settimeout(5)
            data = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
                if len(data) > 1 << 20:
                    return
            if not data:
                return
            try:
                ev = json.loads(data.decode("utf-8"))
            except ValueError:
                return
            if ev.get("cb_type") == "shutdown":
                LOG.info("shutdown requested via control socket")
                self._shutdown.set()
                return
            if ev.get("cb_type") == "ping":
                conn.sendall(b'{"ok":true}')
                return
            ok = self.handle_event(ev)
            # Done-ack for callers that wait (tests, debugging) — the fppd
            # callback never does, so this never holds fppd up.
            try:
                conn.sendall(
                    json.dumps({"ok": bool(ok)}).encode("utf-8") + b"\n"
                )
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _write_state(self, force=False):
        """Status file rendered by status.php. Written only when something
        actually changed (plus a 60s freshness touch) — an idle daemon
        shouldn't rewrite the SD card every few seconds."""
        state = {
            "connected": bool(self.session and self.session._connected.is_set()),
            "playlistActive": self._playlist_active,
            "currentMedia": self._current_media,
            "currentSource": self._current_source,
            "pushed": sorted(self._pushed.values()),
            "registered": sorted(self._registered),
            "roomId": self.cfg.get("roomId"),
            "serverAddr": self.cfg.get("serverAddr"),
            "enabled": cfg_bool(self.cfg.get("enabled", "0")),
            "listenerCount": (
                self.session.listener_count if self.session else 0
            ),
            "nowPlaying": self._np_doc,
        }
        try:
            sig = json.dumps(state, sort_keys=True)
        except (TypeError, ValueError):
            sig = None
        now = time.time()
        if (not force and sig == self._state_sig
                and now - self._state_written < 60):
            return
        state["pid"] = os.getpid()
        state["time"] = now
        try:
            os.makedirs(DATADIR, exist_ok=True)
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, STATE_PATH)
            self._state_sig = sig
            self._state_written = now
        except OSError as e:
            LOG.warning("state write failed: %s", e)

    # -- main --------------------------------------------------------------

    def run(self):
        os.makedirs(DATADIR, exist_ok=True)
        # single instance via socket bind
        try:
            if HAS_UNIX:
                # Any existing socket is stale by definition — the instance
                # lock proves no live daemon can own it.
                if os.path.exists(SOCK_PATH):
                    os.unlink(SOCK_PATH)
                srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                srv.bind(SOCK_PATH)
                try:
                    # fppd's callbacks run as root like us; nobody else has
                    # business on the control channel.
                    os.chmod(SOCK_PATH, 0o600)
                except OSError:
                    pass
            else:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind(("127.0.0.1", 0))
                with open(SOCK_PATH, "w") as f:
                    f.write("127.0.0.1:%d" % srv.getsockname()[1])
            srv.listen(8)
        except OSError as e:
            LOG.error("cannot bind %s: %s (already running?)", SOCK_PATH, e)
            return 1

        with open(PID_PATH, "w") as f:
            f.write(str(os.getpid()))

        LOG.info("falconbeatd started (pid %d), control %s", os.getpid(), SOCK_PATH)

        state_thread_stop = threading.Event()

        def state_writer():
            while not state_thread_stop.wait(2.0):
                self._write_state()

        threading.Thread(target=state_writer, daemon=True).start()
        threading.Thread(target=self._nowplaying_heartbeat, daemon=True).start()
        threading.Thread(target=self._reconcile_loop, daemon=True).start()

        try:
            self._serve_control(srv)
        finally:
            state_thread_stop.set()
            # leave a clean off-air marker so the listener page doesn't sit
            # stale for up to 90s
            try:
                self._set_nowplaying(
                    {"playing": False, "updatedAtMs": epoch_ms()}
                )
            except Exception:
                pass
            if self.session:
                self.session.close()
            self._write_state(force=True)
            srv.close()
            for p in (SOCK_PATH, PID_PATH):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            LOG.info("falconbeatd stopped")
        return 0


def control_connect(timeout=2):
    """Client side of the control channel (also used for the is-alive ping)."""
    if HAS_UNIX:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(SOCK_PATH)
        return s
    with open(SOCK_PATH) as f:
        host, port = f.read().strip().rsplit(":", 1)
    return socket.create_connection((host, int(port)), timeout=timeout)


def _acquire_instance_lock():
    """Exclusive flock held for the life of the process — exactly one
    daemon runs no matter how many launchers fire (postStart, lifecycle
    callback, first event). Returns the open fd to keep, False if another
    daemon holds it, None where flock doesn't exist."""
    if fcntl is None:
        return None
    try:
        fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        return fd
    except OSError:
        return None


def main():
    os.makedirs(LOGDIR, exist_ok=True)
    # WatchedFileHandler re-opens the file each emit, so the daemon keeps
    # logging after FPP's logrotate renames plugin-*.log (create, not
    # copytruncate) instead of writing into the rotated .1 forever.
    handler = logging.handlers.WatchedFileHandler(
        os.path.join(LOGDIR, "plugin-%s.log" % PLUGIN_NAME))
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    # Single instance: the flock is check-and-hold atomically, unlike a
    # socket ping which races between two simultaneous launches.
    os.makedirs(DATADIR, exist_ok=True)
    lock_fd = _acquire_instance_lock()
    if lock_fd is False:
        print("falconbeatd already running (lock held)")
        return 0
    # Fallback for platforms without flock: ping the control socket.
    if lock_fd is None and os.path.exists(SOCK_PATH):
        try:
            s = control_connect()
            s.sendall(b'{"cb_type":"ping"}')
            s.shutdown(socket.SHUT_WR)
            s.recv(64)
            s.close()
            print("falconbeatd already running")
            return 0
        except OSError:
            pass  # stale socket; take over

    daemon = FalconBeatDaemon()
    daemon._instance_lock = lock_fd  # pin the fd open for process life
    try:
        return daemon.run()
    except Exception:
        LOG.exception("falconbeatd crashed")
        raise


if __name__ == "__main__":
    sys.exit(main())
