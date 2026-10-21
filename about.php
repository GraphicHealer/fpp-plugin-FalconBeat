<h2>FalconBeat — Help</h2>

<p>
FalconBeat plays your show's audio on visitors' phones, synchronized to the
FPP sequence clock, using a self-hosted
<a href="https://github.com/freeman-jiang/beatsync" target="_blank">BeatSync</a>
server.
</p>

<h4>How it works</h4>
<ul>
<li>When a playlist starts, every audio file it references is pushed to the
FalconBeat ingest service and registered on the BeatSync room — phones
preload them.</li>
<li>When each media entry starts playing, a PLAY is scheduled on the room with
the FPP playback position plus the server's computed lead time, so phones
seek to the same point in the track.</li>
<li>Pause-type entries, playlist pauses and stops send PAUSE.</li>
<li>A short-lived admin connection per playlist keeps the room alive; it drops
a few seconds after the playlist ends.</li>
</ul>

<h4>Configuration</h4>
<p>
Content Setup &rarr; FalconBeat. You need the BeatSync WS URL, room ID and
creator secret, the ingest URL + token, and the public media URL phones use
to fetch audio.
</p>

<h4>Troubleshooting</h4>
<ul>
<li>Status &rarr; FalconBeat shows daemon/connection state and pushed tracks.</li>
<li>Log file: <code>File Manager &rarr; Logs &rarr; plugin-fpp-plugin-FalconBeat.log</code></li>
<li>"no SCHEDULED_ACTION echo" in the log means the server rejected the PLAY —
usually an unregistered audio source or bad creator secret.</li>
<li>If the BeatSync container restarts mid-show, bindings are re-registered on
reconnect automatically.</li>
</ul>
