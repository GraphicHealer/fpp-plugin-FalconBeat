#!/usr/bin/env python3
"""
FalconBeat daemon — keeps FPP playback mirrored to a FalconBeat room.

Run by FPP (postStart.sh / lifecycle callback). Speaks:

  - FalconBeat admin WS (ws://<server>/ws?roomId=..&creator=..) — held open
    for the duration of a playlist so the room (deleted after 60s idle)
    stays alive and listeners stay attached.
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
import json
import logging
import os
import select
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

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
STATE_PATH = os.path.join(DATADIR, "status.json")
SECRETS_PATH = os.path.join(DATADIR, "secrets")
SETTINGS_PATH = os.path.join(CFGDIR, "plugin." + PLUGIN_NAME)
FPP_STATUS_URL = "http://127.0.0.1/api/fppd/status"

LOG = logging.getLogger("falconbeatd")


# --------------------------------------------------------------------------
# Settings (config/plugin.fpp-plugin-FalconBeat — FPP key=value format)
# --------------------------------------------------------------------------

DEFAULTS = {
    "enabled": "1",
    # LAN address of the FalconBeat edge — one published port carries the
    # admin WS, ingest writes, and the gated HTTP endpoints. Accepts
    # "host", "host:port", or "http(s)://host[:port]".
    "serverAddr": "localhost",
    "roomId": "000000",
    # Shared token: ?creator= on the admin WS, Bearer on /upload/complete,
    # and Bearer on ingest writes — the server's FALCONBEAT_TOKEN.
    "falconbeatToken": "",
    "adminSecret": "",
    # Public base URL devices fetch from — https://host; /media is derived.
    "publicBase": "",
    # Extra milliseconds added to the computed schedule delay when telling
    # devices where in the track to start. Tune if devices run audibly late.
    "nudgeMs": "0",
    # Seconds after a playlist stop before the admin WS drops (room GC'd ~60s
    # later regardless). Small grace lets listeners see the PAUSE land.
    "disconnectGraceSec": "5",
    # fppd blocks in the media callback inside PreparePlay — before the media
    # output exists. The callback waits on the daemon up to this many seconds
    # for push + register + PLAY echo, so tracks can't start before devices
    # are ready. 0 disables gating.
    "gateMaxSec": "10",
    # Milliseconds between the gate releasing and FPP's media output actually
    # starting — subtracted from the device track offset on gated starts.
    "startLagMs": "250",
    # Devices fetch audio over the public internet; when a track was just
    # registered, the server only holds its 3s load-coordination window once
    # PLAY is sent — a multi-MB download may not fit. On gated starts where
    # this URL was registered during this call, wait this long before sending
    # PLAY so the download gets a head start (FPP stays gated the whole time;
    # the wait counts against gateMaxSec). 0 disables.
    "preloadSettleMs": "2500",
}


def load_settings():
    settings = dict(DEFAULTS)
    # config/plugin.* first, then plugindata/secrets (0600) on top —
    # credentials live outside config/ so backups and crash bundles
    # never carry them (PLUGIN_GUIDELINES §14.11).
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
    pub = settings.get("publicBase", "").strip().rstrip("/")
    settings["mediaBase"] = pub + "/media" if pub else ""
    return settings


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

    def __init__(self, url, timeout=10):
        self.url = url
        self.sock, self._recvbuf = self._handshake(url, timeout)
        self._recvbuf = bytearray(self._recvbuf)
        self._send_lock = threading.Lock()
        self.sock.settimeout(None)

    @staticmethod
    def _handshake(url, timeout):
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
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n" % (path, host, port, key)
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
        if self.cfg.get("falconbeatToken"):
            params["creator"] = self.cfg["falconbeatToken"]
        if self.cfg.get("adminSecret"):
            params["admin"] = self.cfg["adminSecret"]
        url = "%s/ws?%s" % (ws_base, urllib.parse.urlencode(params))
        self.ws = MiniWS(url)
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
                w["result"] = msg
            self._echo_cond.notify_all()
            self._echo_waiters = [w for w in self._echo_waiters
                                  if w["result"] is None]

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

    def push_file(self, local_path, media_name):
        """PUT file to ingest -> returns public URL devices will fetch."""
        ingest = self.cfg["httpBase"]
        quoted = urllib.parse.quote(media_name)
        with open(local_path, "rb") as f:
            data = f.read()
        status, resp = self._http(
            "PUT",
            "%s/media/%s" % (ingest, quoted),
            body=data,
            headers={
                "Authorization": "Bearer %s" % self.cfg["falconbeatToken"],
                "Content-Type": "application/octet-stream",
            },
            timeout=120,
        )
        if status >= 300:
            raise RuntimeError("ingest PUT -> HTTP %d: %s" % (status, resp[:200]))
        stored = media_name
        try:
            stored = json.loads(resp).get("stored", media_name)
        except ValueError:
            pass
        base = self.cfg["mediaBase"] or "%s/media" % ingest
        return "%s/%s" % (base, urllib.parse.quote(stored))


# --------------------------------------------------------------------------
# FPP helpers
# --------------------------------------------------------------------------


def find_media_file(name):
    """Resolve a mediaFilename from a playlist entry to an on-disk path."""
    for sub in ("music", "videos", ""):
        p = os.path.join(MEDIADIR, sub, name) if sub else os.path.join(MEDIADIR, name)
        if os.path.isfile(p):
            return p
    return None


def fpp_status():
    """Full status dict from the FPP status API, or None if unreachable."""
    try:
        with urllib.request.urlopen(FPP_STATUS_URL, timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def fpp_position_seconds():
    """Current media position from the FPP status API. Returns (seconds, playing)."""
    st = fpp_status() or {}
    playing = st.get("status_name") == "playing" or st.get("status") == 1
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
        self._registered_at = {}  # url -> monotonic time of (re-)registration
        self._current_source = None
        self._current_media = None
        self._played_urls = set()  # sources that finished — pruned on next PLAY
        self._queue = []           # ordered playlist media names (dupes kept)
        self._queue_idx = -1       # queue position of the current track
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
        self._failed_media = {}  # filename -> epoch of last failed lookup
        self._pb_lock = threading.Lock()  # serialize play()/pause()

    # -- session lifecycle ----------------------------------------------

    def _on_ws_event(self, msg):
        if msg.get("type") == "_disconnected":
            LOG.info("FalconBeat session dropped")
            self._registered.clear()
            self._registered_at.clear()
            if self._playlist_active:
                self._schedule_reconnect()

    def _schedule_reconnect(self):
        def retry():
            delay = 2.0
            while self._playlist_active and not self._shutdown.is_set():
                try:
                    self._connect()
                    return
                except Exception as e:
                    LOG.warning("reconnect failed: %s; retry in %.0fs", e, delay)
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
            if 0 <= self._queue_idx + 1 < len(self._queue):
                nxt_url = self._pushed.get(self._queue[self._queue_idx + 1])
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
                        self._registered_at[url] = time.monotonic()
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
                self._registered_at.clear()

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

    def _set_nowplaying(self, doc):
        self._np_doc = doc
        try:
            self._put_config("nowplaying", doc)
        except Exception as e:
            LOG.warning("nowplaying update failed: %s", e)

    def _nowplaying_heartbeat(self):
        while not self._shutdown.wait(self.NOWPLAYING_HEARTBEAT_S):
            doc = self._np_doc
            if not doc or not doc.get("playing"):
                continue
            doc["updatedAtMs"] = epoch_ms()
            try:
                self._put_config("nowplaying", doc)
            except Exception as e:
                LOG.warning("nowplaying heartbeat failed: %s", e)

    # -- fppd status reconciler ------------------------------------------
    # The callbacks only fire on NEW transitions: a playlist already playing
    # when the daemon comes up (fppd restarted, schedule resumed mid-show,
    # a callback missed entirely) would never reach us. Poll fppd every few
    # seconds and converge — the event handlers dedupe on _current_media so
    # this never double-fires PLAY.

    RECONCILE_PERIOD_S = 3.0
    FAILED_RETRY_S = 60.0

    def _reconcile_loop(self):
        while not self._shutdown.wait(self.RECONCILE_PERIOD_S):
            try:
                self.cfg = load_settings()
                if not cfg_bool(self.cfg.get("enabled", "1")):
                    continue
                st = fpp_status()
                if not st:
                    continue
                status_name = str(st.get("status_name", "")).lower()
                song = st.get("current_song") or ""
                if song in ("0", "none"):
                    song = ""

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
                        except Exception as e:
                            LOG.warning("reconcile PLAY %s failed: %s",
                                        song, e)
                            self._failed_media[song] = epoch_ms()
                    elif status_name == "paused" and self._current_source:
                        self.pause()
                    elif status_name == "playing" and not song \
                            and self._current_source:
                        # sequence-only entry on air — park listeners
                        self.pause()
                elif status_name in ("idle", "stopping"):
                    if self._playlist_active or self._current_source:
                        LOG.info("fppd %s — reconciling to stopped",
                                 status_name)
                        self._playlist_active = False
                        self._queue = []
                        self._queue_idx = -1
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
                    path = find_media_file(media_name)
                    if not path:
                        raise FileNotFoundError(
                            "media file %r not found under %s" % (media_name, MEDIADIR)
                        )
                    LOG.info("pushing %s -> ingest", media_name)
                    url = self._connect().push_file(
                        path, os.path.basename(media_name)
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
                    self._registered_at[url] = time.monotonic()
        return url

    def play(self, media_name, data=None, gated=False):
        with self._pb_lock:
            self._play(media_name, data, gated)

    def _play(self, media_name, data=None, gated=False):
        if media_name == self._current_media:
            return
        self._playlist_active = True
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
        # Room membership check BEFORE ensure_source registers — devices start
        # downloading on SET_AUDIO_SOURCES, so a URL registered during this
        # call means the download only just began.
        known_urls = set(self._registered)
        url = self.ensure_source(media_name)
        just_registered = url not in known_urls
        delay = session.expected_delay_s()
        if gated:
            # fppd is blocked in the media callback. We release it just before
            # serverTimeToExecute so FPP's media output starts at position 0
            # at (almost) the same instant devices start at position 0 — the
            # server's scheduling delay becomes a between-track pause instead
            # of missing intro audio.
            lag = float(self.cfg.get("startLagMs") or 0) / 1000.0
            target = 0.0
            LOG.info("PLAY %s @ 0s (gated; holding ~%.2fs)",
                     media_name, delay)
        else:
            pos, _playing = fpp_position_seconds()
            target = pos + delay
            LOG.info("PLAY %s @ %.2fs (fpp pos %.2f + delay %.2f)",
                     media_name, target, pos, delay)
        # Silence the previous track's tail while devices load the new one.
        old_source = self._current_source
        if old_source and old_source != url:
            try:
                session.send_command(
                    {
                        "type": "PAUSE",
                        "audioSource": old_source,
                        "trackTimeSeconds": 0.0,
                    }
                )
            except Exception as e:
                LOG.warning("pre-play PAUSE of old source failed: %s", e)
        # Freshly registered file + gated start: devices only just learned
        # the URL and download it over WAN. The server's 3s load-coordination
        # clock doesn't start until PLAY arrives — give the download a head
        # start first so devices fit inside that window instead of missing
        # the intro. FPP stays gated the whole time either way.
        settle = float(self.cfg.get("preloadSettleMs") or 0) / 1000.0
        # Fresh means "registered within the settle window" — covers a URL
        # the pre-push thread registered seconds ago at playlist start too,
        # which is exactly when track 1 needs the head start.
        fresh = time.monotonic() - self._registered_at.get(url, 0) < settle
        if gated and settle > 0 and (just_registered or fresh):
            LOG.info("new source %s — settling %.2fs for device download",
                     media_name, settle)
            time.sleep(settle)
        _t = time.monotonic()
        echo = session.send_command(
            {"type": "PLAY", "audioSource": url, "trackTimeSeconds": target}
        )
        LOG.info("[dbg] PLAY->echo %.2fs", time.monotonic() - _t)
        self._current_source = url
        self._current_media = media_name
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
        if 0 <= self._queue_idx + 1 < len(self._queue):
            keep_names.add(self._queue[self._queue_idx + 1])
            # Cursor only just advanced — add "up next" to the now-playing
            # doc (it was pushed early, before the cursor moved).
            doc["next"] = self._queue[self._queue_idx + 1]
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
        exec_ms = echo.get("serverTimeToExecute")
        LOG.info("PLAY confirmed, executes at server ts %s", exec_ms)
        if gated:
            # Hold the gate so FPP's media starts lag-ms before devices fire.
            # Server delay is capped at 3s — cap the hold a bit above that.
            now_ms = session.server_now_ms()
            remaining_s = (exec_ms - now_ms) / 1000.0 if exec_ms else None
            LOG.info(
                "[timing] server_now_ms=%.1f exec_ms=%s remaining_s=%s lag_s=%.3f",
                now_ms, exec_ms, remaining_s, lag,
            )
            if exec_ms and session.best_rtt_ms is not None:
                # NTP offset is measured — trust the server-clock delta.
                wait = remaining_s - lag
            else:
                # No NTP offset yet (or no exec_ms): derive the hold from the
                # delay we computed and the monotonic time since PLAY went out.
                elapsed = time.monotonic() - _t
                wait = delay - elapsed - lag
            wait = min(max(wait, 0.0), 3.5)
            LOG.info("[timing] final gate wait=%.3fs", wait)
            if wait:
                time.sleep(wait)

    def pause(self):
        with self._pb_lock:
            self._pause()

    def _pause(self):
        with self._session_lock:
            session = self.session
        if session is None or self._current_source is None:
            return
        pos, _playing = fpp_position_seconds()
        target = pos + session.expected_delay_s()
        try:
            session.send_command(
                {
                    "type": "PAUSE",
                    "audioSource": self._current_source,
                    "trackTimeSeconds": max(0.0, target),
                }
            )
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
        if not cfg_bool(self.cfg.get("enabled", "1")):
            return True  # nothing to do — release the gate immediately

        try:
            if cb_type == "playlist":
                return self._on_playlist(data)
            elif cb_type == "media":
                return self._on_media(data)
            return True
        except Exception as e:
            LOG.exception("event %s failed: %s", cb_type, e)
            return False

    def _on_playlist(self, data):
        action = str(data.get("Action", "")).lower()
        if action == "start":
            LOG.info("playlist start")
            self._playlist_active = True
            self._queue_fetch_fail.clear()  # fresh playlist — retry the fetch
            if self._disconnect_timer:
                self._disconnect_timer.cancel()
                self._disconnect_timer = None
            self._connect()
            # The callback data is Playlist::GetInfo() — it carries the
            # playlist "name" but never the entry list, so the queue comes
            # from the playlist API (sections in data are kept as a
            # best-effort first try for other FPP variants).
            names = playlist_media_files(data)
            if not names:
                pl = (data.get("name") or data.get("Playlist")
                      or data.get("playlistName"))
                if not pl:
                    st = fpp_status() or {}
                    pl = st.get("current_playlist") or ""
                    if isinstance(pl, dict):
                        pl = pl.get("playlist") or ""
                names = fpp_playlist_files(pl)
            self._queue = names
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
            self._queue = []
            self._queue_idx = -1
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
            for u in urls:
                self._registered_at.pop(u, None)
        if names:
            self._delete_media_files(names)

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
        names = fpp_playlist_files(name)
        if names:
            self._queue = names
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

    def _push_ahead(self, idx):
        """Ensure the playlist entry after queue position idx is uploaded +
        registered — devices then download it while the current track plays,
        so the next gated PLAY doesn't wait on the WAN fetch."""
        if not self._playlist_active or self._shutdown.is_set():
            return
        nxt = idx + 1
        if 0 <= nxt < len(self._queue):
            try:
                self.ensure_source(self._queue[nxt])
            except Exception as e:
                LOG.warning("lookahead push %s failed: %s",
                            self._queue[nxt], e)

    def _on_media(self, data):
        entry_type = str(data.get("type", "")).lower()
        media_name = data.get("Media") or ""
        if entry_type in ("both", "media", "video") and media_name:
            # fresh media -> any pending disconnect is moot while a playlist runs
            try:
                # fppd is blocked in our callback inside PreparePlay — the
                # media output doesn't exist yet, so this is a gated start.
                self.play(media_name, data, gated=True)
                return True
            except Exception as e:
                LOG.exception("PLAY %s failed: %s", media_name, e)
                return False
        elif entry_type in ("pause", "sequence", "event"):
            # non-audio playlist entry: park listeners
            if self._current_source:
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
            # A "wait" event comes from the media callback while fppd is
            # blocked in waitpid() inside PreparePlay — replying releases the
            # gate and lets the track start.
            if ev.get("wait"):
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

    def _write_state(self):
        """Status file rendered by status.php."""
        state = {
            "pid": os.getpid(),
            "time": time.time(),
            "connected": bool(self.session and self.session._connected.is_set()),
            "playlistActive": self._playlist_active,
            "currentMedia": self._current_media,
            "currentSource": self._current_source,
            "pushed": sorted(self._pushed.values()),
            "registered": sorted(self._registered),
            "roomId": self.cfg.get("roomId"),
            "serverAddr": self.cfg.get("serverAddr"),
            "enabled": cfg_bool(self.cfg.get("enabled", "1")),
            "listenerCount": (
                self.session.listener_count if self.session else 0
            ),
            "nowPlaying": self._np_doc,
        }
        try:
            os.makedirs(DATADIR, exist_ok=True)
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, STATE_PATH)
        except OSError as e:
            LOG.warning("state write failed: %s", e)

    # -- main --------------------------------------------------------------

    def run(self):
        os.makedirs(DATADIR, exist_ok=True)
        # single instance via socket bind
        try:
            if HAS_UNIX:
                if os.path.exists(SOCK_PATH):
                    os.unlink(SOCK_PATH)
                srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                srv.bind(SOCK_PATH)
                try:
                    os.chmod(SOCK_PATH, 0o666)
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
            self._write_state()
            if self.session:
                self.session.close()
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


def main():
    os.makedirs(LOGDIR, exist_ok=True)
    logging.basicConfig(
        filename=os.path.join(LOGDIR, "plugin-%s.log" % PLUGIN_NAME),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # already running? -> control socket ping tells us
    if os.path.exists(SOCK_PATH):
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
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())
