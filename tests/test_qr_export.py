import contextlib
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

import taptap_qr as qr
import taptap_save as app


# Synthetic values only. No account or application credentials are fixtures.
ACCOUNT = {'client_id': 'synthetic-game', 'kid': 'synthetic-account-id', 'key': 'synthetic-account-secret'}
CHALLENGE = {'success': True, 'data': {
    'device_code': 'synthetic-device-code', 'qrcode_url': 'https://example.test/synthetic-qr',
    'expires_in': 300, 'interval': 2,
}}
TOKEN = {'success': True, 'data': {'kid': ACCOUNT['kid'], 'mac_key': ACCOUNT['key'], 'mac_algorithm': 'hmac-sha-1'}}


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def pending(state):
    return 400, {'success': False, 'data': {'error': state}}


def archive(members):
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w') as zipped:
        for name, content in members:
            zipped.writestr(name, content)
    return result.getvalue()


class QrTests(unittest.TestCase):
    def authorize(self, responses):
        clock, stdout = Clock(), io.StringIO()
        with patch.object(qr, 'auth_post', side_effect=responses), \
                patch.object(qr, 'print_qr') as render, \
                patch.object(qr.time, 'monotonic', clock.monotonic), \
                patch.object(qr.time, 'sleep', clock.sleep), contextlib.redirect_stdout(stdout):
            result = qr.authorize('synthetic-game')
        return result, clock, stdout.getvalue(), render

    def test_pending_waiting_and_success_do_not_print_credentials(self):
        result, clock, output, render = self.authorize([
            (200, CHALLENGE), pending('authorization_pending'), pending('authorization_waiting'), (200, TOKEN),
        ])
        self.assertEqual(result, ACCOUNT)
        self.assertEqual(clock.sleeps, [2, 2, 2])
        render.assert_called_once_with(CHALLENGE['data']['qrcode_url'])
        for secret in [ACCOUNT['kid'], ACCOUNT['key'], CHALLENGE['data']['device_code']]:
            self.assertNotIn(secret, output)

    def test_slow_down_increases_poll_interval(self):
        _, clock, _, _ = self.authorize([(200, CHALLENGE), pending('slow_down'), (200, TOKEN)])
        self.assertEqual(clock.sleeps, [2, 7])

    def test_expiry_stops_before_another_poll_without_renewing(self):
        challenge = {'success': True, 'data': dict(CHALLENGE['data'], expires_in=3)}
        clock = Clock()
        with patch.object(qr, 'auth_post', side_effect=[(200, challenge), pending('authorization_pending')]) as post, \
                patch.object(qr, 'print_qr'), patch.object(qr.time, 'monotonic', clock.monotonic), \
                patch.object(qr.time, 'sleep', clock.sleep), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(app.ExportError, '过期'):
                qr.authorize('synthetic-game')
        self.assertEqual(post.call_count, 2)

    def test_denial_and_unsupported_algorithm_stop(self):
        bad_token = {'success': True, 'data': dict(TOKEN['data'], mac_algorithm='unsupported')}
        for response in [pending('access_denied'), (200, bad_token), (200, {'success': True, 'data': {}})]:
            with self.subTest(response=response):
                with self.assertRaises(app.ExportError):
                    self.authorize([(200, CHALLENGE), response])

    def test_server_errors_have_a_retry_limit(self):
        response = 503, {'success': False, 'data': {'error': 'server_error'}}
        with self.assertRaisesRegex(app.ExportError, '连续失败'):
            self.authorize([(200, CHALLENGE), response, response, response])

    def test_time_bounds_and_https_qr(self):
        for overrides in [{'expires_in': float('nan')}, {'interval': -1}, {'expires_in': True},
                          {'expires_in': 100000}, {'qrcode_url': 'javascript:synthetic'}]:
            challenge = {'success': True, 'data': dict(CHALLENGE['data'], **overrides)}
            with self.subTest(overrides=overrides):
                with self.assertRaises(app.ExportError):
                    self.authorize([(200, challenge)])

    def test_oauth_http400_body_is_parsed_without_logging(self):
        raw = json.dumps(pending('authorization_pending')[1]).encode()
        error = urllib.error.HTTPError('https://example.test/private-url', 400, 'private-reason', {}, io.BytesIO(raw))
        stdout = io.StringIO()
        with patch.object(qr.urllib.request, 'build_opener') as build, contextlib.redirect_stdout(stdout):
            build.return_value.open.side_effect = error
            status, payload = qr.auth_post('/oauth2/v1/token', {'code': 'synthetic-secret'})
            request = build.return_value.open.call_args.args[0]
        self.assertEqual(status, 400)
        self.assertEqual(payload['data']['error'], 'authorization_pending')
        self.assertEqual(request.full_url, qr.AUTH_ORIGIN + '/oauth2/v1/token')
        self.assertEqual(stdout.getvalue(), '')

    def test_oauth_unexpected_endpoint_and_oversized_response(self):
        with self.assertRaises(app.ExportError):
            qr.auth_post('/cloud-save/v1/update', {})
        response = io.BytesIO(b'x' * (qr.MAX_RESPONSE + 1))
        response.code = 200
        with patch.object(qr.urllib.request, 'build_opener') as build:
            build.return_value.open.return_value = response
            with self.assertRaises(app.ExportError):
                qr.auth_post('/oauth2/v1/token', {})

    def test_no_arguments_runs_qr_flow_without_reading_a_local_account(self):
        with patch.object(app.sys, 'argv', ['taptap_save.py']), patch.object(qr, 'run') as run:
            self.assertEqual(app.main(), 0)
        run.assert_called_once_with(None)

    def test_ctrl_c_is_cleanly_reported(self):
        stderr = io.StringIO()
        with patch.object(app.sys, 'argv', ['taptap_save.py', 'qr-export']), \
                patch.object(qr, 'run', side_effect=KeyboardInterrupt), contextlib.redirect_stderr(stderr):
            self.assertEqual(app.main(), 130)
        self.assertNotIn('Traceback', stderr.getvalue())

    def test_script_entry_sanitizes_qr_errors_as_well_as_imported_main(self):
        script = Path(app.__file__).resolve()
        source = (
            "import sys,runpy; sys.modules['qrcode']=None; "
            "sys.argv=['taptap_save.py','qr-export']; "
            "runpy.run_path(sys.argv[0],run_name='__main__')"
        )
        result = subprocess.run([sys.executable, '-c', source], cwd=script.parent,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('二维码依赖', result.stderr)
        self.assertNotIn('Traceback', result.stderr)


class ExportTests(unittest.TestCase):
    def test_repeated_exports_preserve_zip_sav_checksums_and_no_credentials(self):
        raw = archive([('GameSave_00_GD.sav', b'game-data'), ('GameSave_00_PZ.sav', b'paired-data')])
        save = {'host': 'example.test', 'save_path': '/private-url?signature=synthetic-secret',
                'save_size': len(raw), 'modified_time': 1, 'summary': 'private-identity'}
        with tempfile.TemporaryDirectory() as name, patch.object(app, 'list_saves', return_value=[save]), \
                patch.object(app.urllib.request, 'build_opener') as build, contextlib.redirect_stdout(io.StringIO()):
            build.return_value.open.side_effect = lambda *a, **k: io.BytesIO(raw)
            first = qr.export_account(ACCOUNT, 'synthetic-app-secret', Path(name) / 'exports')
            second = qr.export_account(ACCOUNT, 'synthetic-app-secret', Path(name) / 'exports')
            self.assertNotEqual(first, second)
            self.assertEqual((first / 'save-001.zip').read_bytes(), raw)
            self.assertEqual((first / 'SData/GameSave_00_GD.sav').read_bytes(), b'game-data')
            self.assertEqual(stat.S_IMODE((first / 'SData/GameSave_00_GD.sav').stat().st_mode), 0o600)
            summary = (first / 'run-info.json').read_text()
            self.assertEqual(json.loads(summary)['results'][0]['sav_files'], 2)
            for marker in ['synthetic-account', 'synthetic-app-secret', 'synthetic-secret', 'private-identity', 'private-url']:
                for path in first.glob('*.json'):
                    self.assertNotIn(marker, path.read_text())

    def test_empty_account_does_not_create_export(self):
        with tempfile.TemporaryDirectory() as name, patch.object(app, 'list_saves', return_value=[]):
            dest = Path(name) / 'exports'
            with self.assertRaises(app.ExportError):
                qr.export_account(ACCOUNT, 'synthetic-app-secret', dest)
            self.assertFalse(dest.exists())

    def test_unsafe_archive_writes_no_extracted_files(self):
        for filename in ['../outside.sav', '/outside.sav', 'C:/outside.sav', 'dir\\outside.sav', 'CON.sav', 'unsafe.txt']:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as name:
                dest = Path(name) / 'SData'
                raw = archive([('valid.sav', b'data'), (filename, b'bad')])
                with self.assertRaises(app.ExportError):
                    qr.extract_savs(io.BytesIO(raw), dest)
                self.assertFalse(dest.exists())

    def test_case_collisions_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            raw = archive([('a.sav', b'data'), ('A.sav', b'other')])
            with self.assertRaises(app.ExportError):
                qr.extract_savs(io.BytesIO(raw), Path(name) / 'SData')
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as zipped:
                link = zipfile.ZipInfo('link.sav')
                link.create_system = 3
                link.external_attr = 0o120777 << 16
                zipped.writestr(link, 'outside')
            with self.assertRaises(app.ExportError):
                qr.extract_savs(io.BytesIO(buffer.getvalue()), Path(name) / 'SData')

    def test_main_error_never_exposes_private_url(self):
        error = urllib.error.HTTPError('https://example.test/?signature=private-marker', 401, 'private-marker', {}, None)
        stderr = io.StringIO()
        with patch.object(app.sys, 'argv', ['taptap_save.py', 'qr-export']), \
                patch.object(qr, 'run', side_effect=error), contextlib.redirect_stderr(stderr):
            self.assertEqual(app.main(), 1)
        self.assertIn('401', stderr.getvalue())
        self.assertNotIn('private-marker', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
