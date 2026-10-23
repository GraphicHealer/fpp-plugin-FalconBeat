<h2>FalconBeat — Help</h2>

<p>
FalconBeat plays your show's audio on visitors' devices, synchronized to the
FPP sequence clock, using a self-hosted
<a href="https://github.com/GraphicHealer/FalconBeat" target="_blank">FalconBeat</a>
server.
</p>

<h4>How it works</h4>
<ul>
<li>The room only ever holds two tracks: the one playing and the next. Each
track's upload + registration happens during the previous song, so devices
download it early — a skipped or finished track leaves the room (and the
server's media dir) right away.</li>
<li>When each media entry starts, FPP's start is held briefly while devices
confirm the audio is loaded, then devices and FPP start position 0 at the
same scheduled instant — a short, intentional gap between tracks.</li>
<li>Pause-type entries, playlist pauses and stops send PAUSE.</li>
<li>A short-lived admin connection per playlist keeps the room alive; it drops
a few seconds after the playlist ends.</li>
</ul>

<h4>Configuration</h4>
<p>
Content Setup &rarr; FalconBeat. You need the server's LAN address (one
published port carries the admin socket, uploads and ingest) and the
shared server token. Devices resolve audio URLs against the listener
page's own origin, so no public address is configured here.
</p>

<h4>Troubleshooting</h4>
<ul>
<li>Status &rarr; FalconBeat shows daemon/connection state and pushed tracks.</li>
<li>Log file: <code>File Manager &rarr; Logs &rarr; plugin-fpp-plugin-FalconBeat.log</code></li>
<li>"no SCHEDULED_ACTION echo" in the log means the server rejected the PLAY —
usually an unregistered audio source or bad creator secret.</li>
<li>If the FalconBeat server restarts mid-show, bindings are re-registered on
reconnect automatically.</li>
</ul>
