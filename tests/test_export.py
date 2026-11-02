import base64
import contextlib
import hashlib
import hmac
import io
import json
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

import taptap_save as app

# All values below are synthetic and cannot authenticate to a real service.
CLIENT = "synthetic-demo-client"
KEY = "synthetic-application-key-00000001"


def login(openid="synthetic-user", client=CLIENT):
    payload = {"success": True, "data": {"openid": openid, "sdk_token": {
        "kid": "synthetic-token-id", "mac_key": "synthetic-account-key", "mac_algorithm": "hmac-sha-1"
    }}}
    return {"request": {"url": f"https://{app.API_HOST}/auth/v1/login-by-token?client_id={client}"},
            "response": {"content": {"text": json.dumps(payload)}}}


def metadata(strings):
    table = b""
    data = b""
    for value in strings:
        raw = value.encode()
        table += struct.pack("<II", len(raw), len(data))
        data += raw
    return struct.pack("<6I", 0xFAB11BAF, 27, 24, len(table), 24 + len(table), len(data)) + table + data


class MemoryResponse(io.BytesIO):
    status = 200


class ExportTests(unittest.TestCase):
    def test_rfc4231_hmac_sha256_vector(self):
        expected = bytes.fromhex("5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843")
        self.assertEqual(base64.b64decode(app.app_signature("Jefe", b"what do ya want for nothing?")), expected)

    def test_canonical_request_preserves_encoded_query_and_body(self):
        message = app.app_message("POST", "https://example.test/a?q=a%20b", [
            ("X-Tap-Ts", "10"), ("Authorization", "ignored"),
            ("x-tap-nonce", "n"), ("X-Tap-Sign", "excluded")], b'{"a":1}')
        self.assertEqual(message, b'POST\n/a?q=a%20b\nx-tap-nonce:n\nx-tap-ts:10\n{"a":1}\n')

    def test_account_response_base64_and_origin_filter(self):
        entry = login()
        content = entry["response"]["content"]
        content["text"] = base64.b64encode(content["text"].encode()).decode()
        content["encoding"] = "base64"
        attacker = login("other")
        attacker["request"]["url"] = attacker["request"]["url"].replace(app.API_HOST, "example.test")
        self.assertEqual(app.select_account([attacker, entry])["client_id"], CLIENT)

    def test_mixed_accounts_rejected(self):
        with self.assertRaises(app.ExportError):
            app.select_account([login(), login("different-user")])

    def test_missing_login_response_rejected(self):
        with self.assertRaises(app.ExportError):
            app.select_account([{"request": login()["request"]}])

    def test_metadata_bounds_rejected(self):
        broken = bytearray(metadata([KEY]))
        struct.pack_into("<I", broken, 16, 9999999)
        with self.assertRaises(app.ExportError):
            app.metadata_candidates(broken)

    def test_recover_token_from_synthetic_capture(self):
        url = f"https://{app.API_HOST}/sdk-core/v1/gatekeeper?client_id={CLIENT}"
        body = '{"platform":"test"}'
        signed = b'POST\n/sdk-core/v1/gatekeeper?client_id=synthetic-demo-client\nx-tap-nonce:n\nx-tap-ts:1\n{"platform":"test"}\n'
        signature = base64.b64encode(hmac.new(KEY.encode(), signed, hashlib.sha256).digest()).decode()
        entry = {"request": {"url": url, "method": "POST", "postData": {"text": body}, "headers": [
            {"name": "X-Tap-Ts", "value": "1"}, {"name": "X-Tap-Nonce", "value": "n"},
            {"name": "X-Tap-Sign", "value": signature}]}}
        self.assertEqual(app.recover_token([entry], metadata(["irrelevant-string-value", KEY]), CLIENT), KEY)
        with self.assertRaises(app.ExportError):
            app.recover_token([entry], metadata(["irrelevant-string-value"]), CLIENT)

    def test_mac_signature_uses_exact_request_target(self):
        account = {"kid": "demo", "key": "synthetic-key"}
        url = "https://example.test/a?q=1"
        expected = base64.b64encode(hmac.new(b"synthetic-key", b"10\nn\nGET\n/a?q=1\nexample.test\n443\n\n", hashlib.sha1).digest()).decode()
        self.assertEqual(app.mac_authorization(account, url, timestamp=10, nonce="n"),
                         f'MAC id="demo",ts="10",nonce="n",mac="{expected}"')

    def test_private_write_cannot_overwrite_or_follow_symlink(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            app.private_write(root / "secret", b"original")
            self.assertEqual(stat.S_IMODE((root / "secret").stat().st_mode), 0o600)
            (root / "link").symlink_to(root / "secret")
            for path in [root / "secret", root / "link"]:
                with self.assertRaises(FileExistsError):
                    app.private_write(path, b"changed")
            self.assertEqual((root / "secret").read_bytes(), b"original")

    def test_save_url_cannot_switch_origin(self):
        for path in ["https://other.test/file", "//other.test/file"]:
            with self.assertRaises(app.ExportError):
                app.save_url({"host": "https://storage.test", "save_path": path})
        self.assertEqual(app.save_url({"host": "storage.test", "save_path": "/save?signature=synthetic"}),
                         "https://storage.test/save?signature=synthetic")

    def test_export_retains_no_signed_url_or_identity(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zipped:
            zipped.writestr("GameSave_00_GD.sav", b"synthetic data")
            zipped.writestr("GameSave_00_PZ.sav", b"synthetic pair")
        raw = buffer.getvalue()
        save = {"host": "storage.test", "save_path": "/file?signature=private-marker",
                "save_size": len(raw), "modified_time": 100, "summary": "private-user-marker"}
        with tempfile.TemporaryDirectory() as name, patch.object(app.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = MemoryResponse(raw)
            with contextlib.redirect_stdout(io.StringIO()):
                app.download_save(save, Path(name), 1)
            report = (Path(name) / "save-001.json").read_text()
            self.assertNotIn("private-marker", report)
            self.assertNotIn("private-user-marker", report)
            self.assertTrue(json.loads(report)["zip_crc_verified"])
            self.assertEqual((Path(name) / "save-001.zip").read_bytes(), raw)

    def test_download_size_mismatch_does_not_save(self):
        with tempfile.TemporaryDirectory() as name, patch.object(app.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = MemoryResponse(b"short")
            with self.assertRaises(app.ExportError):
                app.download_save({"host": "storage.test", "save_path": "/file", "save_size": 100}, Path(name), 1)
            self.assertEqual(list(Path(name).iterdir()), [])

    def test_remote_apk_rejects_full_download_response(self):
        response = MemoryResponse(b"not-a-range")
        response.headers = {}
        with patch.object(app.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(app.ExportError):
                app.RemoteZIP("https://example.test/demo.apk")

    def test_api_redirect_is_not_followed(self):
        self.assertIsNone(app.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test/"))


if __name__ == "__main__":
    unittest.main()
