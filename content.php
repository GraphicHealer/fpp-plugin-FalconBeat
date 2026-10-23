<?php
/*
 * FalconBeat configuration page (Content Setup menu).
 *
 * Settings are stored the FPP way: config/plugin.fpp-plugin-FalconBeat
 * via WriteSettingToFile, read back with ReadSettingFromFile.
 *
 * The FalconBeat edge is ONE published port: serverAddr carries the admin
 * WS, ingest writes, and the gated HTTP endpoints; one FALCONBEAT_TOKEN is
 * ?creator= on the WS and Bearer on /upload/complete + ingest writes.
 * Advanced/Developer sections follow FPP's uiLevel (0 Basic / 1 Advanced /
 * 3 Developer) — hidden fields keep their stored values on save.
 *
 * Saving (or the Test button) probes: edge /health, the shared Bearer
 * token, the WS upgrade, the upload/complete gate, and the public media
 * URL.
 */

$pluginName = 'fpp-plugin-FalconBeat';

// Credentials live under plugindata/ mode 0600, never in config/ (§14.11).
$secretKeys = array('falconbeatToken', 'adminSecret');

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

// Sections honor FPP's uiLevel: 0 Basic, 1 Advanced, 3 Developer.
// Hidden sections keep their stored values — the save loop skips keys
// that were not rendered.
$uiLevel = isset($settings['uiLevel']) ? intval($settings['uiLevel']) : 0;

$sections = array(
    array('level' => 0, 'title' => '', 'fields' => array(
        // key, label, type, help text
        array('enabled',         'Enabled',          'checkbox', 'Master switch — when off, playback events are ignored.'),
        array('serverAddr',      'Server (LAN)',     'text',     'FalconBeat server on your LAN: falconbeat.local or falconbeat.local:9080 (one published port carries everything).'),
        array('publicBase',      'Public URL',       'text',     'e.g. https://falconbeat.example.com — where devices open the listener page and fetch /media.'),
        array('falconbeatToken', 'Server Token',     'password', 'FALCONBEAT_TOKEN on the server — one shared secret for admin WS, uploads and ingest writes.'),
    )),
    array('level' => 1, 'title' => 'Advanced', 'fields' => array(
        array('roomId',          'Room ID',          'text',     '6-digit room code. The listener page joins 000000 by default (?room= overrides it) — change both together.'),
        array('adminSecret',     'Admin Secret',     'password', 'Optional: demo-mode ?admin= secret. Usually left blank.'),
        array('gateMaxSec',      'Sync Gate (sec)',  'text',     'Hold each track\'s start until devices confirm loaded (max seconds). 0 = off.'),
        array('preloadSettleMs', 'Preload Settle (ms)', 'text',  'When a track is pushed at play time, wait this long before PLAY so devices can download over WAN. Counts against the sync gate. 0 = off.'),
    )),
    array('level' => 3, 'title' => 'Developer', 'fields' => array(
        array('nudgeMs',         'Extra Delay (ms)', 'text',     'Added to the computed schedule delay for the device track offset. 0 = auto.'),
        array('startLagMs',      'FPP Start Lag (ms)', 'text',   'How long FPP takes to actually start media after the gate releases — subtracted from the device offset. Tune by ear.'),
        array('disconnectGraceSec', 'Disconnect grace (s)', 'text', 'Seconds the admin socket is held after playlist stop.'),
    )),
);

$fields = array();
$rendered = array();
foreach ($sections as $sec) {
    foreach ($sec['fields'] as $f) {
        $fields[] = $f;
        if ($sec['level'] <= $uiLevel) $rendered[$f[0]] = true;
    }
}

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

/**
 * Derive wire URLs from the simple fields — same rules as falconbeatd's
 * _split_addr/load_settings. Returns [httpBase, wsBase, mediaBase].
 */
function fb_bases($v) {
    $addr = rtrim(trim($v['serverAddr']), '/');
    $secure = false;
    if (strpos($addr, '://') !== false) {
        $u = parse_url($addr);
        $secure = in_array(isset($u['scheme']) ? $u['scheme'] : '',
                           array('https', 'wss'));
        $addr = (isset($u['host']) ? $u['host'] : '')
              . (isset($u['port']) ? ':' . $u['port'] : '');
    }
    $pub = rtrim(trim($v['publicBase']), '/');
    return array(
        ($secure ? 'https' : 'http') . '://' . $addr,
        ($secure ? 'wss' : 'ws') . '://' . $addr,
        $pub === '' ? '' : $pub . '/media',
    );
}

function fb_tests($v) {
    // $v: array of saved setting values
    $results = array();
    list($http, $ws, $media) = fb_bases($v);
    $token = $v['falconbeatToken'];
    $bearer = array('Authorization: Bearer ' . $token);

    // 1. edge reachability — /health proxies to beatsync through Caddy
    list($code, $err) = fb_http('GET', $http . '/health');
    $results[] = array('FalconBeat reachable', $code === 200,
        $code ? "HTTP $code" : $err);

    // 2. shared token — PUT with deliberately invalid JSON: ingest checks
    //    auth before the body, so 400 = accepted, 401 = rejected.
    if ($token === '') {
        $results[] = array('Server token', false,
            'empty — nothing saved yet; type it above and Save & Test');
    } else {
        $auth = array_merge($bearer, array('Content-Type: application/json'));
        list($code, $err) = fb_http('PUT', $http . '/config/show', $auth, 'x');
        $ok = ($code === 400); // invalid-JSON past the auth gate
        $results[] = array('Server token', $ok,
            $code === 401 ? 'unauthorized — check FALCONBEAT_TOKEN'
              : ($code === 400 ? 'accepted' : ($code ? "HTTP $code" : $err)));
    }

    // 3. WS upgrade (room auto-creates; secret validity shows up as admin
    //    status, not the handshake — a 101 means the socket works)
    $q = http_build_query(array(
        'roomId' => $v['roomId'], 'username' => 'fpp-config-test',
        'clientId' => 'cfgtest', 'creator' => $token,
        'admin' => $v['adminSecret']));
    list($ok, $detail) = fb_ws_probe($ws, $q);
    $results[] = array('FalconBeat WebSocket', $ok, $detail);

    // 4. upload/complete gate — Caddy checks the Bearer before proxying:
    //    403 = token rejected, anything else = past the gate.
    if ($token !== '') {
        list($code, $err) = fb_http('GET', $http . '/upload/complete', $bearer);
        $results[] = array('Upload gate', $code !== 403,
            $code === 403 ? 'forbidden — check FALCONBEAT_TOKEN'
              : ($code ? "HTTP $code" : $err));
    }

    // 5. public media URL — any HTTP answer (even 404 before the first
    //    track) proves the public route works; a transport error doesn't.
    if ($media !== '') {
        list($code, $err) = fb_http('GET', $media . '/nowplaying.json');
        $results[] = array('Public media URL', $code > 0,
            $code ? "HTTP $code" . ($code === 404 ? ' (no track pushed yet — fine)' : '')
                  : $err);
    }

    return $results;
}

$saved = false;
$saveErr = null;
$testResults = null;
$formVals = null; // POSTed values, when the form should re-render them
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (isset($_POST['fb_save'])) {
        $secrets = array();
        foreach ($fields as $f) {
            $key = $f[0];
            // Not rendered at this uiLevel — leave the stored value alone.
            if (!isset($rendered[$key])) continue;
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
        if (file_put_contents($sfile, $body, LOCK_EX) === false) {
            $saveErr = 'Could not write ' . $sfile .
                ' — check plugindata permissions. Non-secret settings were saved.';
        }
        @chmod($sfile, 0600);
        $saved = true;
        $testResults = 'auto'; // run the checks with the just-saved values
    }
    if (isset($_POST['fb_test'])) {
        // Test what is IN THE FORM — the saved values may not exist yet,
        // and re-render the fields with what was typed so nothing is lost.
        $formVals = array();
        foreach ($fields as $f) {
            $key = $f[0];
            if (!isset($rendered[$key])) {
                // Hidden at this uiLevel — test the stored value.
                $formVals[$key] = fb_get($key);
                continue;
            }
            $formVals[$key] = ($f[2] === 'checkbox')
                ? (isset($_POST[$key]) ? '1' : '0')
                : (isset($_POST[$key]) ? trim($_POST[$key]) : '');
        }
        $testResults = fb_tests($formVals);
    }
}

if ($testResults === 'auto') {
    $v = array();
    foreach ($fields as $f) $v[$f[0]] = fb_get($f[0]);
    $testResults = fb_tests($v);
}
?>

<h2>FalconBeat</h2>
<p>
Mirrors FPP playback to a FalconBeat room so visitors' devices play the show
audio in sync. Audio files are pushed to the ingest service at playlist start,
then PLAY/PAUSE is issued over the FalconBeat admin socket as media entries
run.
</p>

<?php if ($saved): ?>
<div class="alert alert-success">Settings saved. Changes take effect on the next playlist/media event.</div>
<?php endif; ?>
<?php if ($saveErr): ?>
<div class="alert alert-danger"><?php echo htmlspecialchars($saveErr); ?></div>
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
<?php foreach ($sections as $sec):
    if ($sec['level'] > $uiLevel) continue;
    if ($sec['title'] !== ''): ?>
<tr><td colspan="2"><h4><?php echo htmlspecialchars($sec['title']); ?></h4></td></tr>
<?php endif;
    foreach ($sec['fields'] as $f):
        $key = $f[0]; $label = $f[1]; $type = $f[2]; $help = $f[3];
        $val = ($formVals !== null && array_key_exists($key, $formVals))
            ? $formVals[$key]
            : fb_get($key, isset($defaults[$key]) ? $defaults[$key] : '');
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
<?php endforeach; endforeach; ?>
</table>
<button type="submit" name="fb_save" value="1" class="btn btn-primary">Save &amp; Test</button>
<button type="submit" name="fb_test" value="1" class="btn btn-default">Test these settings (no save)</button>
</form>
