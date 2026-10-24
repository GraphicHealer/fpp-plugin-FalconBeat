<?php
/*
 * FalconBeat configuration page (Input/Output Setup menu).
 *
 * Fields are declared in settings.json and rendered/saved by FPP's own
 * machinery: PrintSettingGroup draws the native rows (tip icons, uiLevel
 * badges, instant per-field save via SetPluginSetting into
 * config/plugin.fpp-plugin-FalconBeat) and gates groups on uiLevel —
 * 0 Basic / 1 Advanced / 3 Developer. There is no form and no save loop.
 *
 * Exception: the credential field keeps the native row but overrides
 * onChange to post here instead — §14.11 requires secrets in
 * plugindata/secrets (0600), never in config/ where backups carry them.
 *
 * Second exception: FPP's declarative types have no textarea, so the two
 * multiline Show Info fields (description, links) render as custom rows
 * below and post here too. Newlines store escaped as \n in the key=value
 * file (WriteSettingToFile refuses real ones); the daemon unescapes.
 *
 * The FalconBeat edge is ONE published port: serverAddr carries the admin
 * WS, ingest writes, and the gated HTTP endpoints; one FALCONBEAT_TOKEN is
 * an Authorization Bearer everywhere — never a query parameter (a proxy
 * access log would capture it).
 *
 * The Test button probes the saved values: edge /health, the shared
 * Bearer token, the WS upgrade, the upload/complete gate, and /media.
 */

$pluginName = 'fpp-plugin-FalconBeat';

// Credentials live under plugindata/ mode 0600, never in config/ — the
// JSON backup carries config/plugin.* in clear (§14.11). The settings.json
// rows stay declarative for masking/layout; their onChange posts here.
$secretKeys = array('falconbeatToken');

// Multiline show fields rendered as custom textarea rows (no declarative
// textarea type) — saved into the same plugin.* file with \n escapes.
$textKeys = array('showDescription', 'showLinks');

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

// Write a credential into plugindata/secrets (0600), preserving any other
// keys, and blank the same key in config/plugin.<name> so nothing secret
// sits where a JSON backup or crash bundle would carry it.
function fb_secret_save($key, $value) {
    global $pluginName;
    $file = fb_secrets_path();
    $vals = array();
    if (is_readable($file)) {
        foreach (file($file) as $line) {
            $line = trim($line);
            if ($line === '' || strpos($line, '=') === false) continue;
            list($k, $v) = explode('=', $line, 2);
            $vals[trim($k)] = trim(trim($v), '"');
        }
    }
    $vals[$key] = $value;
    $dir = dirname($file);
    if (!is_dir($dir)) @mkdir($dir, 0755, true);
    $body = '';
    foreach ($vals as $k => $v)
        $body .= $k . ' = "' . str_replace('"', '', $v) . "\"\n";
    $ok = file_put_contents($file, $body, LOCK_EX) !== false;
    @chmod($file, 0600);
    if (function_exists('WriteSettingToFile'))
        WriteSettingToFile($key, '', $pluginName);
    return $ok;
}

// Migration: a build briefly saved credentials into config/plugin.<name>
// through the declarative fields. Move any residue into the secrets file
// (secrets win if both exist — that file is the current write target).
foreach ($secretKeys as $sk) {
    $pv = function_exists('ReadSettingFromFile') ? ReadSettingFromFile($sk, $pluginName) : null;
    if ($pv !== null && $pv !== false && $pv !== '') {
        if (fb_secret_get($sk) === null) fb_secret_save($sk, $pv);
        else if (function_exists('WriteSettingToFile')) WriteSettingToFile($sk, '', $pluginName);
    }
}

// Saved-value lookup for the connection test. plugindata/secrets is
// authoritative for credentials; the plugin settings file is only a
// fallback so a pre-migration token still tests correctly.
function fb_get($key, $default = '') {
    global $pluginName, $secretKeys, $settings;
    if (in_array($key, $secretKeys)) {
        $v = fb_secret_get($key);
        if ($v !== null && $v !== '') return $v;
    }
    if (function_exists('ReadSettingFromFile')) {
        $v = ReadSettingFromFile($key, $pluginName);
        if ($v !== false && $v !== null) return $v;
    } else {
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
function fb_ws_probe($wsUrl, $query, $headers = array(), $timeout = 4) {
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
           "Sec-WebSocket-Key: $key\r\nSec-WebSocket-Version: 13\r\n";
    foreach ($headers as $h) $req .= $h . "\r\n";
    $req .= "\r\n";
    fwrite($fp, $req);
    $line = fgets($fp);
    fclose($fp);
    if ($line === false) return array(false, 'no response');
    return array(strpos($line, ' 101') !== false, trim($line));
}

/**
 * Derive wire URLs from serverAddr — same rules as falconbeatd's
 * _split_addr/load_settings. Returns [httpBase, wsBase].
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
    return array(
        ($secure ? 'https' : 'http') . '://' . $addr,
        ($secure ? 'wss' : 'ws') . '://' . $addr,
    );
}

function fb_tests($v) {
    // $v: array of saved setting values
    $results = array();
    list($http, $ws) = fb_bases($v);
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
            'empty — set it above (fields save as you change them)');
    } else {
        $auth = array_merge($bearer, array('Content-Type: application/json'));
        list($code, $err) = fb_http('PUT', $http . '/config/show', $auth, 'x');
        $ok = ($code === 400); // invalid-JSON past the auth gate
        $results[] = array('Server token', $ok,
            $code === 401 ? 'unauthorized — check FALCONBEAT_TOKEN'
              : ($code === 400 ? 'accepted' : ($code ? "HTTP $code" : $err)));
    }

    // 3. WS upgrade (room auto-creates; secret validity shows up as admin
    //    status, not the handshake — a 101 means the socket works). The
    //    token rides the Authorization header, never the query string.
    $q = http_build_query(array(
        'roomId' => '000000', 'username' => 'fpp-config-test',
        'clientId' => 'cfgtest'));
    list($ok, $detail) = fb_ws_probe($ws, $q,
        array('Authorization: Bearer ' . $token));
    $results[] = array('FalconBeat WebSocket', $ok, $detail);

    // 4. upload/complete gate — Caddy checks the Bearer before proxying:
    //    403 = token rejected, anything else = past the gate.
    if ($token !== '') {
        list($code, $err) = fb_http('GET', $http . '/upload/complete', $bearer);
        $results[] = array('Upload gate', $code !== 403,
            $code === 403 ? 'forbidden — check FALCONBEAT_TOKEN'
              : ($code ? "HTTP $code" : $err));
    }

    // 5. media route — devices fetch /media/* from this same edge; any
    //    HTTP answer (even 404 before the first track) proves it's wired.
    list($code, $err) = fb_http('GET', $http . '/media/nowplaying.json');
    $results[] = array('Media route', $code > 0,
        $code ? "HTTP $code" . ($code === 404 ? ' (no track pushed yet — fine)' : '')
              : $err);

    return $results;
}

// Credential save endpoint — the settings.json rows' onChange posts here
// instead of FPP's SetPluginSetting, keeping secrets out of config/.
if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['fb_secret'])) {
    $key = $_POST['fb_secret'];
    $val = isset($_POST['fb_secret_value']) ? $_POST['fb_secret_value'] : '';
    $ok = in_array($key, $secretKeys) && fb_secret_save($key, $val);
    if (!headers_sent()) header('Content-Type: application/json');
    echo json_encode(array('ok' => $ok));
    exit;
}

// Textarea save endpoint — writes into config/plugin.<name> with newlines
// escaped to \n (the key=value format is one setting per line).
if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['fb_setting'])) {
    $key = $_POST['fb_setting'];
    $val = isset($_POST['fb_setting_value']) ? $_POST['fb_setting_value'] : '';
    $ok = in_array($key, $textKeys) && function_exists('WriteSettingToFile');
    if ($ok) {
        $val = str_replace('"', '', str_replace(array("\r\n", "\r", "\n"), '\n', $val));
        $ok = WriteSettingToFile($key, $val, $pluginName);
    }
    if (!headers_sent()) header('Content-Type: application/json');
    echo json_encode(array('ok' => (bool)$ok));
    exit;
}

// Display helper for the textarea rows — reverses the stored \n escapes.
function fb_multiline($key) {
    return str_replace('\n', "\n", fb_get($key));
}

$testResults = null;
if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['fb_test'])) {
    $v = array(
        'serverAddr'      => fb_get('serverAddr'),
        'falconbeatToken' => fb_get('falconbeatToken'),
    );
    $testResults = fb_tests($v);
}
?>

<h2>FalconBeat</h2>
<p>
Mirrors FPP playback to a FalconBeat room so visitors' devices play the show
audio in sync. Audio files are pushed to the server as playlist entries run,
and PLAY/PAUSE is issued over the FalconBeat admin socket. Fields save as
you change them — changes take effect on the next playlist/media event.
</p>

<div id="global" class="settings">
<?php
if (function_exists('LoadPluginSettings')) LoadPluginSettings($pluginName);
PrintSettingGroup('FalconBeat', '', '', 1, $pluginName);
PrintSettingGroup('FalconBeatShow', '', '', 1, $pluginName);
?>
<div class="row" style="margin-top:1rem;">
    <div class="col-md-3 col-lg-2"><b>Description</b>
        <div class="text-muted" style="font-size:0.85em;">A paragraph about the
        show — donations, charity, how it works.</div>
    </div>
    <div class="col-md-9 col-lg-10">
        <textarea id="showDescription" rows="3" class="form-control"
                  onchange="fbSaveSetting('showDescription', this)"
        ><?php echo htmlspecialchars(fb_multiline('showDescription')); ?></textarea>
    </div>
</div>
<div class="row" style="margin-top:1rem;">
    <div class="col-md-3 col-lg-2"><b>Links</b>
        <div class="text-muted" style="font-size:0.85em;">One per line:
        <code>Label = https://url</code> — shown as pills in Show Info.</div>
    </div>
    <div class="col-md-9 col-lg-10">
        <textarea id="showLinks" rows="3" class="form-control"
                  placeholder="Donate = https://example.com/donate"
                  onchange="fbSaveSetting('showLinks', this)"
        ><?php echo htmlspecialchars(fb_multiline('showLinks')); ?></textarea>
    </div>
</div>
<?php
PrintSettingGroup('FalconBeatAdvanced', '', '', 1, $pluginName);
PrintSettingGroup('FalconBeatDeveloper', '', '', 1, $pluginName);
?>
</div>

<script>
// The credential rows render blank by design — their values live in
// plugindata/secrets (0600), not the plugin settings file. onChange posts
// the new value here; the placeholder shows whether a value is on file.
function fbSaveSecret(key, el) {
    var fd = new FormData();
    fd.append('fb_secret', key);
    fd.append('fb_secret_value', el.value);
    fetch(location.pathname + location.search, { method: 'POST', body: fd })
        .then(function (r) { return r.json(); })
        .then(function (d) {
            if (window.$ && $.jGrowl) $.jGrowl(d.ok ? key + ' saved' : 'Save failed — check plugindata permissions');
            if (d.ok) el.placeholder = '\u2022\u2022\u2022\u2022\u2022\u2022 saved \u2014 type to replace';
        });
}
// Same instant-save pattern for the custom textarea rows — posts back to
// this page which writes the (newline-escaped) value into the plugin config.
function fbSaveSetting(key, el) {
    var fd = new FormData();
    fd.append('fb_setting', key);
    fd.append('fb_setting_value', el.value);
    fetch(location.pathname + location.search, { method: 'POST', body: fd })
        .then(function (r) { return r.json(); })
        .then(function (d) {
            if (window.$ && $.jGrowl) $.jGrowl(d.ok ? key + ' saved' : 'Save failed');
        });
}
<?php foreach ($secretKeys as $sk): if (fb_secret_get($sk) !== null): ?>
document.getElementById('<?php echo $sk; ?>').placeholder =
    '\u2022\u2022\u2022\u2022\u2022\u2022 saved \u2014 type to replace';
<?php endif; endforeach; ?>
</script>

<fieldset>
<legend>Connection test</legend>
<p class="text-muted">Probes the saved Server address and token.</p>
<form method="post" action="">
<button type="submit" name="fb_test" value="1" class="buttons">Test Connection</button>
</form>
<?php if (is_array($testResults)): ?>
<table class="table table-striped" style="max-width: 50rem; margin-top: 0.5rem;">
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
</fieldset>

<fieldset>
<legend>Signage QR</legend>
<p class="text-muted">Encodes the Public Listener URL above — put it on your
yard sign so visitors can scan-and-listen. Updates as you edit the field.</p>
<canvas id="fbQrCanvas" width="216" height="216"
        style="background:#fff;border-radius:8px;max-width:216px;"></canvas>
<div id="fbQrLabel" class="text-muted" style="margin-top:0.4rem;"></div>
<script>
function fbRenderQr() {
    var canvas = document.getElementById('fbQrCanvas');
    var label = document.getElementById('fbQrLabel');
    var input = document.getElementById('showPublicUrl');
    var url = input && input.value.trim() !== ''
        ? input.value.trim()
        : <?php echo json_encode(fb_get('showPublicUrl')); ?>;
    var ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (typeof qrcode === 'undefined') {
        label.textContent = 'qrcode.js failed to load';
        return;
    }
    if (!url) {
        label.textContent = 'Set the Public Listener URL above to render a QR code.';
        return;
    }
    try {
        var qr = qrcode(0, 'M');
        qr.addData(url);
        qr.make();
        var count = qr.getModuleCount();
        var tile = Math.floor(canvas.width / (count + 8));
        var off = Math.floor((canvas.width - tile * count) / 2);
        ctx.fillStyle = '#fff';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = '#000';
        for (var r = 0; r < count; r++)
            for (var c = 0; c < count; c++)
                if (qr.isDark(r, c))
                    ctx.fillRect(off + c * tile, off + r * tile, tile, tile);
        label.textContent = url;
    } catch (e) {
        label.textContent = 'Could not encode URL: ' + e;
    }
}
fbRenderQr();
var fbQrInput = document.getElementById('showPublicUrl');
if (fbQrInput) fbQrInput.addEventListener('input', fbRenderQr);
</script>
</fieldset>
