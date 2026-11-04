"""Interactive QR authorization followed by a local, read-only save export."""

import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import taptap_save as api

AUTH_ORIGIN = 'https://accounts.tapapis.cn'
MAX_RESPONSE = 1_000_000
MAX_AUTH_SECONDS = 900


def auth_post(path, fields):
    """Use only the two documented OAuth endpoints, with redirects disabled."""
    if path not in ('/oauth2/v1/device/code', '/oauth2/v1/token'):
        raise api.ExportError('不支持的授权接口。')
    request = urllib.request.Request(
        AUTH_ORIGIN + path,
        data=urllib.parse.urlencode(fields).encode(),
        headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
        method='POST',
    )
    try:
        response = urllib.request.build_opener(api.NoRedirect()).open(request, timeout=20)
    except urllib.error.HTTPError as error:
        # OAuth pending states use HTTP 400. Never log the body or request URL.
        response = error
    with response:
        status = response.code
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise api.ExportError('授权响应超过大小限制。')
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
        raise api.ExportError('授权服务器返回了不兼容的响应。')
    return status, payload


def positive_seconds(value, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise api.ExportError('授权响应的时间参数无效。')
    if not math.isfinite(value) or value <= 0 or value > maximum:
        raise api.ExportError('授权响应的时间参数无效。')
    return float(value)


def print_qr(url):
    try:
        import qrcode
    except ImportError:
        raise api.ExportError('缺少二维码依赖，请通过 export.command、export.sh 或 export.bat 启动。') from None
    # Render locally. No QR service receives the authorization URL.
    qr = qrcode.QRCode(border=4)
    qr.add_data(url)
    try:
        qr.make(fit=True)
    except qrcode.exceptions.DataOverflowError:
        raise api.ExportError('授权二维码超过终端显示容量，已停止。') from None
    qr.print_ascii(invert=True)
    sys.stdout.flush()


def authorize(client_id):
    status, payload = auth_post('/oauth2/v1/device/code', {
        'client_id': client_id, 'response_type': 'device_code', 'scope': 'public_profile',
    })
    if status != 200 or payload.get('success') is not True:
        raise api.ExportError(f'申请二维码失败（HTTP {status}）。未保存授权响应。')
    data = payload['data']
    for key in ('device_code', 'qrcode_url'):
        if not isinstance(data.get(key), str) or not 0 < len(data[key]) <= 4096:
            raise api.ExportError('授权响应缺少有效的二维码或设备码。')
    # Refuse unexpected URL schemes before rendering a server-provided URL.
    if urllib.parse.urlsplit(data['qrcode_url']).scheme != 'https':
        raise api.ExportError('授权二维码未使用 HTTPS，已停止。')
    expires = positive_seconds(data.get('expires_in'), MAX_AUTH_SECONDS)
    interval = max(2.0, positive_seconds(data.get('interval', 5), MAX_AUTH_SECONDS))
    deadline = time.monotonic() + expires
    print(f'请用 TapTap App 扫码并确认《潜水员戴夫》授权（有效期 {int(expires)} 秒）。', flush=True)
    print('终端请保持足够宽，避免二维码换行；按 Ctrl+C 可取消。', flush=True)
    print_qr(data['qrcode_url'])
    last_state = None
    server_errors = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= interval:
            raise api.ExportError('二维码已过期，请重新运行；未自动申请新的授权。')
        time.sleep(interval)
        status, payload = auth_post('/oauth2/v1/token', {
            'grant_type': 'device_token', 'client_id': client_id,
            'secret_type': 'hmac-sha-1', 'code': data['device_code'],
        })
        token = payload['data']
        if status == 200 and payload.get('success') is True:
            if token.get('mac_algorithm', 'hmac-sha-1') != 'hmac-sha-1':
                raise api.ExportError('授权令牌使用了不兼容的签名算法。')
            if not all(isinstance(token.get(k), str) and token[k] for k in ('kid', 'mac_key')):
                raise api.ExportError('授权成功，但令牌字段不完整。')
            print('扫码授权成功，正在查询云存档……', flush=True)
            # Account credentials live in memory only; do not persist the OAuth payload.
            return {'client_id': client_id, 'kid': token['kid'], 'key': token['mac_key']}
        state = token.get('error')
        if status == 400 and state in ('authorization_pending', 'authorization_waiting'):
            server_errors = 0
            if state != last_state:
                print('等待扫码……' if state == 'authorization_pending' else '已扫码，等待手机确认授权……', flush=True)
                last_state = state
        elif status == 400 and state == 'slow_down':
            interval += 5
        elif state == 'server_error' and status in (400, 500, 502, 503):
            server_errors += 1
            if server_errors >= 3:
                raise api.ExportError('授权服务器连续失败，已停止，请稍后重试。')
        else:
            messages = {
                'access_denied': '扫码授权已取消或被拒绝。',
                'authorization_declined': '扫码授权已取消或被拒绝。',
                'invalid_grant_code': '二维码已过期或失效，请重新运行。',
                'invalid_grant': '本次授权已失效，请重新运行。',
                'invalid_client': '游戏应用参数已失效，请检查项目更新。',
            }
            raise api.ExportError(messages.get(state, f'扫码授权失败（HTTP {status}），未输出响应内容。'))


def extract_savs(archive, destination):
    """Validate all members before extracting; never call extractall()."""
    with zipfile.ZipFile(archive) as zipped:
        infos = zipped.infolist()
        if len(infos) > 1000 or sum(i.file_size for i in infos) > 200_000_000:
            raise api.ExportError('存档 ZIP 超过解压限制，原始文件已保留。')
        names, files = set(), []
        for info in infos:
            name = info.filename
            path = PurePosixPath(name)
            mode = (info.external_attr >> 16) & 0o170000
            if (path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name
                    or mode == stat.S_IFLNK or not path.parts):
                raise api.ExportError('存档 ZIP 包含不安全路径，原始文件已保留。')
            # Reject Windows aliases, trailing dots/spaces and case collisions too.
            for part in path.parts:
                stem = part.split('.')[0].upper()
                if part.endswith((' ', '.')) or stem in {'CON', 'PRN', 'AUX', 'NUL'} or (
                        len(stem) == 4 and stem[:3] in ('COM', 'LPT') and stem[3] in '123456789'):
                    raise api.ExportError('存档 ZIP 包含不兼容路径，原始文件已保留。')
            if info.is_dir():
                continue
            canonical = path.as_posix().casefold()
            if path.suffix != '.sav' or canonical in names:
                raise api.ExportError('存档 ZIP 含非 sav 文件或重复路径，原始文件已保留。')
            names.add(canonical)
            files.append((info, path))
        if not files or zipped.testzip() is not None:
            raise api.ExportError('ZIP 无有效存档或 CRC 校验失败，原始文件已保留。')
        destination.mkdir(mode=0o700, exist_ok=False)
        for info, path in files:
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            api.private_write(target, zipped.read(info))
    return len(files)


def export_account(account, client_token, output_root):
    """Called after explicit authorization; no publishing or game-directory writes."""
    saves = api.list_saves(account, client_token)
    if not saves:
        raise api.ExportError('该账号没有云存档。请先在游戏内手动上传进度后重试。')
    if output_root.is_symlink():
        raise api.ExportError('导出目录不能为符号链接。')
    output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = dt.datetime.now().astimezone()
    output = output_root / now.strftime('%Y%m%d-%H%M%S-%f')
    output.mkdir(mode=0o700, exist_ok=False)
    results = []
    for index, save in enumerate(saves, 1):
        api.download_save(save, output, index)
        metadata_path = output / f'save-{index:03d}.json'
        report = json.loads(metadata_path.read_text(encoding='utf-8'))
        archive = output / report['file']
        if hashlib.sha256(archive.read_bytes()).hexdigest() != report['sha256']:
            raise api.ExportError('落盘 SHA-256 校验失败，导出文件已保留。')
        if report['zip_crc_verified']:
            destination = output / 'SData' if len(saves) == 1 else output / f'save-{index:03d}' / 'SData'
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            report['sav_files'] = extract_savs(archive, destination)
        results.append(report)
    summary = {'exported_at': now.isoformat(), 'cloud_saves': len(results), 'results': results}
    api.private_write(output / 'run-info.json', json.dumps(summary, ensure_ascii=False, indent=2).encode())
    count = sum(r.get('sav_files', 0) for r in results)
    print(f'导出完成：{len(results)} 组云存档，{count} 个 .sav 文件。', flush=True)
    print('导出时间：' + now.isoformat(), flush=True)
    print('保存路径：' + str(output.resolve()), flush=True)
    return output


def run(output_root=None):
    # Check dependency before networking or requesting an expiring QR code.
    try:
        import qrcode  # noqa: F401
    except ImportError:
        raise api.ExportError('缺少二维码依赖，请通过 export.command、export.sh 或 export.bat 启动。') from None
    from dave_profile import load_profile
    os.umask(0o077)
    root = Path(__file__).resolve().parent
    profile = load_profile()
    account = authorize(profile['client_id'])
    return export_account(account, profile['client_token'], output_root or root / 'exports')
