<?php
/*
 * FalconBeat status page (Status/Control menu).
 * Renders the daemon's status.json written under plugindata/.
 */

$pluginName = 'fpp-plugin-FalconBeat';
global $settings;
$mediaDir = isset($settings['mediaDirectory']) ? $settings['mediaDirectory'] : '/home/fpp/media';
$stateFile = $mediaDir . '/plugindata/' . $pluginName . '/status.json';
$pidFile   = $mediaDir . '/plugindata/' . $pluginName . '/falconbeatd.pid';

$state = null;
if (is_readable($stateFile)) {
    $state = json_decode(file_get_contents($stateFile), true);
}

$running = false;
if (is_readable($pidFile)) {
    $pid = intval(trim(file_get_contents($pidFile)));
    if ($pid > 0 && file_exists('/proc/' . $pid)) $running = true;
}

function fb_h($s) { return htmlspecialchars((string)$s); }
?>

<h2>FalconBeat Status</h2>

<table class="table table-striped">
<tr><td><b>Daemon</b></td>
    <td><?php echo $running ? '<span class="text-success">running</span>'
                             : '<span class="text-danger">not running</span>'; ?></td></tr>
<?php if ($state): ?>
<tr><td><b>Enabled</b></td><td><?php echo $state['enabled'] ? 'yes' : 'no'; ?></td></tr>
<tr><td><b>BeatSync</b></td><td><?php echo fb_h($state['beatsyncUrl']); ?>
    room <?php echo fb_h($state['roomId']); ?> —
    <?php echo $state['connected'] ? '<span class="text-success">connected</span>'
                                   : '<span class="text-muted">disconnected</span>'; ?></td></tr>
<tr><td><b>Playlist active</b></td><td><?php echo $state['playlistActive'] ? 'yes' : 'no'; ?></td></tr>
<tr><td><b>Listeners</b></td><td><?php echo fb_h(isset($state['listenerCount']) ? $state['listenerCount'] : '0'); ?>
    <small class="text-muted">connected phones (non-admin)</small></td></tr>
<tr><td><b>Now playing</b></td><td><?php echo fb_h($state['currentMedia'] ?: '—'); ?></td></tr>
<?php if (isset($state['nowPlaying']) && is_array($state['nowPlaying'])):
    $np = $state['nowPlaying']; ?>
<tr><td><b>Now-playing doc</b></td><td>
    <?php if (!empty($np['playing'])):
        $bits = array_filter(array(
            isset($np['title']) ? $np['title'] : null,
            isset($np['artist']) ? '— ' . $np['artist'] : null,
            isset($np['album']) ? '(' . $np['album'] . ')' : null));
        echo fb_h(implode(' ', $bits) ?: ($np['filename'] ?? 'playing'));
    else: ?>
        <span class="text-muted">off air</span>
    <?php endif; ?>
    <?php if (!empty($np['updatedAtMs'])): ?>
        <small class="text-muted">updated <?php echo fb_h(round((microtime(true)*1000 - $np['updatedAtMs'])/1000)); ?>s ago</small>
    <?php endif; ?>
</td></tr>
<?php endif; ?>
<tr><td><b>Audio source</b></td><td><code><?php echo fb_h($state['currentSource'] ?: '—'); ?></code></td></tr>
<tr><td><b>Pushed tracks</b></td><td><?php echo count($state['pushed']); ?></td></tr>
<tr><td><b>Registered on room</b></td><td><?php echo count($state['registered']); ?></td></tr>
<tr><td><b>Last update</b></td><td><?php echo fb_h(date('Y-m-d H:i:s', (int)$state['time'])); ?></td></tr>
<?php else: ?>
<tr><td colspan="2"><em>No daemon status file yet — start FPPD or reload the plugin.</em></td></tr>
<?php endif; ?>
</table>

<?php
// Show the listener link if configured
$cfgFile = (isset($settings['configDirectory']) ? $settings['configDirectory'] : '/home/fpp/media/config')
    . '/plugin.' . $pluginName;
$listener = '';
if (is_readable($cfgFile)) {
    foreach (file($cfgFile) as $line) {
        if (strpos(trim($line), 'listenerUrl') === 0) {
            $listener = trim(trim(explode('=', $line, 2)[1]), '"');
        }
    }
}
if ($listener !== ''): ?>
<p><b>Listener page:</b> <a href="<?php echo fb_h($listener); ?>" target="_blank"><?php echo fb_h($listener); ?></a></p>
<?php endif; ?>

<p class="text-muted">
Log: <code>logs/plugin-<?php echo fb_h($pluginName); ?>.log</code>
(Config under Content Setup &rarr; FalconBeat)
</p>
