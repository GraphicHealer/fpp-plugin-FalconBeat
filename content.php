<?php
/*
 * FalconBeat configuration page (Content Setup menu).
 *
 * Settings are stored the FPP way: config/plugin.fpp-plugin-FalconBeat
 * via WriteSettingToFile, read back with ReadSettingFromFile.
 */

$pluginName = 'fpp-plugin-FalconBeat';

$fields = array(
    // key, label, type, help text
    array('enabled',        'Enabled',            'checkbox', 'Master switch — when off, playback events are ignored.'),
    array('beatsyncUrl',    'BeatSync WS URL',    'text',     'e.g. ws://falconbeat.local:8080 — LAN address of the BeatSync server (phones do not use this).'),
    array('roomId',         'Room ID',            'text',     '6-digit room code, e.g. 000000.'),
    array('creatorSecret',  'Creator Secret',     'password', 'CREATOR_SECRET on the BeatSync server (?creator= admin auth). LAN-only.'),
    array('adminSecret',    'Admin Secret',       'password', 'Optional: demo-mode ?admin= secret. Usually left blank.'),
    array('ingestUrl',      'Ingest URL',         'text',     'e.g. http://falconbeat.local:8090 — LAN media ingest service.'),
    array('ingestToken',    'Ingest Token',       'password', 'Bearer token for ingest PUTs. LAN-only.'),
    array('mediaPublicUrl', 'Public Media URL',   'text',     'e.g. https://falconbeat.example.com/media — where phones fetch audio. Must end with /media.'),
    array('listenerUrl',    'Listener Page URL',  'text',     'Optional: public URL of the listener web app, shown on the status page.'),
    array('nudgeMs',        'Extra Delay (ms)',   'text',     'Added to the computed schedule delay for the phone track offset. 0 = auto.'),
    array('disconnectGraceSec', 'Disconnect grace (s)', 'text', 'Seconds the admin socket is held after playlist stop.'),
);

function fb_get($key, $default = '') {
    global $pluginName;
    if (function_exists('ReadSettingFromFile')) {
        $v = ReadSettingFromFile($key, $pluginName);
        if ($v !== false && $v !== null) return $v;
    }
    // fallback: parse the settings file directly
    global $settings;
    $cfgDir = isset($settings['configDirectory']) ? $settings['configDirectory'] : '/home/fpp/media/config';
    $file = $cfgDir . '/plugin.' . $pluginName;
    if (is_readable($file)) {
        foreach (file($file) as $line) {
            $line = trim($line);
            if ($line === '' || strpos($line, '=') === false) continue;
            list($k, $v) = explode('=', $line, 2);
            if (trim($k) === $key) return trim(trim($v), '"');
        }
    }
    return $default;
}

$saved = false;
if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['fb_save'])) {
    foreach ($fields as $f) {
        $key = $f[0];
        if ($f[2] === 'checkbox') {
            $value = isset($_POST[$key]) ? '1' : '0';
        } else {
            $value = isset($_POST[$key]) ? trim($_POST[$key]) : '';
        }
        if (function_exists('WriteSettingToFile')) {
            WriteSettingToFile($key, $value, $pluginName);
        } else {
            // direct write fallback
            global $settings;
            $cfgDir = isset($settings['configDirectory']) ? $settings['configDirectory'] : '/home/fpp/media/config';
            $file = $cfgDir . '/plugin.' . $pluginName;
            $lines = is_readable($file) ? file($file) : array();
            $found = false;
            foreach ($lines as $i => $line) {
                if (strpos(trim($line), $key . '=') === 0) {
                    $lines[$i] = $key . ' = "' . str_replace('"', '', $value) . '"' . "\n";
                    $found = true;
                }
            }
            if (!$found) $lines[] = $key . ' = "' . str_replace('"', '', $value) . '"' . "\n";
            file_put_contents($file, implode('', $lines), LOCK_EX);
        }
    }
    $saved = true;
}
?>

<h2>FalconBeat</h2>
<p>
Mirrors FPP playback to a BeatSync room so visitors' phones play the show audio
in sync. Audio files are pushed to the FalconBeat ingest service at playlist
start, then PLAY/PAUSE is issued over the BeatSync admin socket as media
entries run.
</p>

<?php if ($saved): ?>
<div class="alert alert-success">Settings saved. Changes take effect on the next playlist/media event.</div>
<?php endif; ?>

<form method="post" action="">
<table class="table table-striped" style="max-width: 720px;">
<?php foreach ($fields as $f):
    $key = $f[0]; $label = $f[1]; $type = $f[2]; $help = $f[3];
    $val = fb_get($key, $key === 'enabled' ? '1' : ($key === 'roomId' ? '000000' : ''));
?>
<tr>
    <td style="width: 200px;"><b><?php echo htmlspecialchars($label); ?></b></td>
    <td>
        <?php if ($type === 'checkbox'): ?>
        <input type="checkbox" name="<?php echo $key; ?>" value="1" <?php echo ($val === '1') ? 'checked' : ''; ?>>
        <?php else: ?>
        <input type="<?php echo $type; ?>" name="<?php echo $key; ?>"
               value="<?php echo htmlspecialchars($val); ?>" class="form-control" autocomplete="off">
        <?php endif; ?>
        <small class="text-muted"><?php echo htmlspecialchars($help); ?></small>
    </td>
</tr>
<?php endforeach; ?>
</table>
<button type="submit" name="fb_save" value="1" class="btn btn-primary">Save</button>
</form>
