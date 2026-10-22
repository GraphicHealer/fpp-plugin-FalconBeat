<?php
/*
 * FalconBeat configuration page (Content Setup menu).
 *
 * Settings are stored the FPP way: config/plugin.fpp-plugin-FalconBeat
 * via WriteSettingToFile, read back with ReadSettingFromFile.
 *
 * Saving (or the Test button) probes: ingest /health + bearer token,
 * BeatSync /health + WS upgrade, and the public media URL.
 */

$pluginName = 'fpp-plugin-FalconBeat';

// Credentials live under plugindata/ mode 0600, never in config/ (§14.11).
$secretKeys = array('creatorSecret', 'adminSecret', 'ingestToken');

function fb_secrets_path() {
    global $settings, $pluginName;
    $dir = isset($settings['mediaDirectory']) ? $settings['mediaDirectory'] : '/home/fpp/media';
    return $dir . '/plugindata/' . $pluginName . '/secrets';
}

function fb_secret_get($key) {
    $file = fb_secrets_path();
    if (is_readable($file)) {
        foreach (file($file) as $line) {
            $line = trim($line);
            if ($line === '' || strpos($line, '=') === false) continue;
            list($k, $v) = explode('=', $line, 2);
            if (trim($k) === $key) return trim(trim($v), '"');
        }
    }
    return null;
}

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
    array('gateMaxSec',     'Sync Gate (sec)',    'text',     'Hold each track\'s start until phones confirm loaded (max seconds). 0 = off.'),
    array('startLagMs',     'FPP Start Lag (ms)', 'text',     'How long FPP takes to actually start media after the gate releases — subtracted from the phone offset. Tune by ear.'),
    array('preloadSettleMs','Phone Preload Settle (ms)', 'text','When a track is pushed at play time, wait this long before PLAY so phones can download over WAN. Counts against the sync gate. 0 = off.'),
    array('disconnectGraceSec', 'Disconnect grace (s)', 'text', 'Seconds the admin socket is held after playlist stop.'),
);

$defaults = array(
    'enabled' => '1',
    'roomId' => '000000',
    'gateMaxSec' => '10',
    'startLagMs' => '250',
    'preloadSettleMs' => '2500',
);

function fb_get($key, $default = '') {
    global $pluginName, $secretKeys;
    if (in_array($key, $secretKeys)) {
        $v = fb_secret_get($key);
        if ($v !== null && $v !== '') return $v;
        // fall through — config may still hold a pre-migration value
    }
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

/**
 * Minimal HTTP probe. Returns array(httpCode|0, errorString).
 * Prefers curl (always present in FPP's PHP), falls back to streams.
 */
function fb_http($method, $url, $headers = array(), $body = null, $timeout = 4) {
    if (function_exists('curl_init')) {
        $ch = curl_init($url);
        curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $method);
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
        curl_setopt($ch, CURLOPT_TIMEOUT, $timeout);
        curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, $timeout);
        curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
        curl_setopt($ch, CURLOPT_SSL_VERIFYPEER, true);
        if ($body !== null) curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
        curl_exec($ch);
        $code = curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
        $err = curl_error($ch);
        curl_close($ch);
        if ($code === 0) return array(0, $err ?: 'connection failed');
        return array($code, '');
    }
    // streams fallback
    $h = implode("\r\n", $headers);
    $ctx = stream_context_create(array('http' => array(
        'method' => $method, 'header' => $h, 'content' => $body,
        'timeout' => $timeout, 'ignore_errors' => true,
    )));
    $resp = @file_get_contents($url, false, $ctx);
    if ($resp === false) return array(0, 'connection failed');
    $code = 0;
    if (isset($http_response_header[0]) && preg_match('/\s(\d{3})\s/', $http_response_header[0], $m))
        $code = (int)$m[1];
    return array($code, '');
}

/**
 * WS upgrade probe — checks the server answers 101 Switching Protocols.
 * ws:// only (direct LAN); returns 'skip' for wss.
 */
function fb_ws_probe($wsUrl, $query, $timeout = 4) {
    $u = parse_url($wsUrl);
    if (!$u || !isset($u['host'])) return array(false, 'bad URL');
    if ($u['scheme'] !== 'ws') return array(false, 'skipped (wss — check in browser)');
    $port = isset($u['port']) ? $u['port'] : 80;
    $fp = @fsockopen($u['host'], $port, $errno, $errstr, $timeout);
    if (!$fp) return array(false, $errstr ?: 'connection refused');
    stream_set_timeout($fp, $timeout);
    $key = base64_encode(random_bytes(16));
    $path = '/ws?' . $query;
    $req = "GET $path HTTP/1.1\r\nHost: {$u['host']}:$port\r\n" .
           "Upgrade: websocket\r\nConnection: Upgrade\r\n" .
           "Sec-WebSocket-Key: $key\r\nSec-WebSocket-Version: 13\r\n\r\n";
    fwrite($fp, $req);
    $line = fgets($fp);
    fclose($fp);
    if ($line === false) return array(false, 'no response');
    return array(strpos($line, ' 101') !== false, trim($line));
}

function fb_tests($v) {
    // $v: array of saved setting values
    $results = array();

    // 1. ingest reachability
    $ingest = rtrim($v['ingestUrl'], '/');
    list($code, $err) = fb_http('GET', $ingest . '/health');
    $results[] = array('Ingest reachable', $code === 200,
        $code ? "HTTP $code" : $err);

    // 2. ingest token — PUT with deliberately invalid JSON: auth is checked
    //    before the body is parsed, so 400 = token accepted, 401 = rejected.
    //    Nothing is written either way.
    $auth = array('Authorization: Bearer ' . $v['ingestToken'],
                  'Content-Type: application/json');
    list($code, $err) = fb_http('PUT', $ingest . '/config/show', $auth, 'x');
    $ok = ($code === 400); // invalid-JSON past the auth gate
    $results[] = array('Ingest token', $ok,
        $code === 401 ? 'unauthorized — check INGEST_TOKEN'
          : ($code === 400 ? 'accepted' : ($code ? "HTTP $code" : $err)));

    // 3. beatsync HTTP /health
    $httpBase = preg_replace('/^ws/', 'http', rtrim($v['beatsyncUrl'], '/'));
    list($code, $err) = fb_http('GET', $httpBase . '/health');
    $results[] = array('BeatSync reachable', $code === 200,
        $code ? "HTTP $code" : $err);

    // 4. beatsync WS upgrade (room auto-creates; secret validity shows up as
    //    admin status, not the handshake — a 101 means the socket works)
    $q = http_build_query(array(
        'roomId' => $v['roomId'], 'username' => 'fpp-config-test',
        'clientId' => 'cfgtest', 'creator' => $v['creatorSecret'],
        'admin' => $v['adminSecret']));
    list($ok, $detail) = fb_ws_probe($v['beatsyncUrl'], $q);
    $results[] = array('BeatSync WebSocket', $ok, $detail);

    // 5. public media URL — any HTTP answer (even 404 before the first track)
    //    proves the public route works; a transport error means it doesn't.
    if ($v['mediaPublicUrl'] !== '') {
        list($code, $err) = fb_http('GET',
            rtrim($v['mediaPublicUrl'], '/') . '/nowplaying.json');
        $results[] = array('Public media URL', $code > 0,
            $code ? "HTTP $code" . ($code === 404 ? ' (no track pushed yet — fine)' : '')
                  : $err);
    }

    return $results;
}

$saved = false;
$testResults = null;
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (isset($_POST['fb_save'])) {
        $secrets = array();
        foreach ($fields as $f) {
            $key = $f[0];
            if ($f[2] === 'checkbox') {
                $value = isset($_POST[$key]) ? '1' : '0';
            } else {
                $value = isset($_POST[$key]) ? trim($_POST[$key]) : '';
            }
            if (in_array($key, $secretKeys)) {
                $secrets[$key] = $value;
                $value = ''; // clear any legacy value out of config/
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
        $sfile = fb_secrets_path();
        $sdir = dirname($sfile);
        if (!is_dir($sdir)) @mkdir($sdir, 0755, true);
        $body = '';
        foreach ($secrets as $k => $v)
            $body .= $k . ' = "' . str_replace('"', '', $v) . "\"\n";
        file_put_contents($sfile, $body, LOCK_EX);
        @chmod($sfile, 0600);
        $saved = true;
        $testResults = 'auto'; // run the checks with the just-saved values
    }
    if (isset($_POST['fb_test'])) {
        $testResults = 'manual'; // probe whatever is currently saved
    }
}

if ($testResults !== null) {
    $v = array();
    foreach ($fields as $f) $v[$f[0]] = fb_get($f[0]);
    $testResults = fb_tests($v);
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

<?php if (is_array($testResults)): ?>
<h4>Connection test</h4>
<table class="table table-striped">
<?php foreach ($testResults as $r): ?>
<tr>
    <td><b><?php echo htmlspecialchars($r[0]); ?></b></td>
    <td><?php echo $r[1]
        ? '<span class="text-success">&#10003; ok</span>'
        : '<span class="text-danger">&#10007; failed</span>'; ?>
        <small class="text-muted"><?php echo htmlspecialchars($r[2]); ?></small></td>
</tr>
<?php endforeach; ?>
</table>
<?php endif; ?>

<form method="post" action="">
<table class="table table-striped">
<?php foreach ($fields as $f):
    $key = $f[0]; $label = $f[1]; $type = $f[2]; $help = $f[3];
    $val = fb_get($key, isset($defaults[$key]) ? $defaults[$key] : '');
?>
<tr>
    <td><b><?php echo htmlspecialchars($label); ?></b></td>
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
<button type="submit" name="fb_save" value="1" class="btn btn-primary">Save &amp; Test</button>
<button type="submit" name="fb_test" value="1" class="btn btn-default">Test saved settings</button>
</form>
