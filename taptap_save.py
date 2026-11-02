#!/usr/bin/env python3
"""Read-only TapTap cloud-save export using a local HAR and client token."""

import argparse
import base64
import binascii
import datetime as dt
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

API_HOST = "tapsdk.tapapis.cn"
METADATA_PATH = "assets/bin/Data/Managed/Metadata/global-metadata.dat"
MAX_SAVE = 50_000_000
MAX_METADATA = 64_000_000


class ExportError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def private_write(path, content):
    """Never overwrite an existing file, including a symlink."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)


def load_har(path):
    if path.stat().st_size > 100_000_000:
        raise ExportError("HAR exceeds the 100 MB input limit")
    entries = json.loads(path.read_text(encoding="utf-8"))["log"]["entries"]
    if not isinstance(entries, list):
        raise ExportError("HAR log.entries must be an array")
    return entries


def response_json(entry):
    content = entry.get("response", {}).get("content", {})
    text = content.get("text", "")
    if content.get("encoding") == "base64":
        text = base64.b64decode(text, validate=True).decode("utf-8")
    return json.loads(text)


def client_id_from_url(url):
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("client_id", [])
    return values[0] if len(values) == 1 else None


def select_account(entries, requested_client=None):
    accounts = {}
    for entry in entries:
        url = urllib.parse.urlsplit(entry.get("request", {}).get("url", ""))
        if url.scheme != "https" or url.netloc != API_HOST or url.path != "/auth/v1/login-by-token":
            continue
        client_id = client_id_from_url(url.geturl())
        if not client_id or (requested_client and requested_client != client_id):
            continue
        try:
            payload = response_json(entry)
            data = payload["data"]
            token = data["sdk_token"]
            kid, key = token["kid"], token["mac_key"]
            if payload.get("success") is not True or not isinstance(kid, str) or not isinstance(key, str) or not kid or not key:
                continue
            if token.get("mac_algorithm", "hmac-sha-1") != "hmac-sha-1":
                raise ExportError("This tool currently supports hmac-sha-1 account tokens only")
            # Refreshes for one account replace earlier tokens. Mixed-account HARs fail closed.
            identity = (client_id, data.get("openid") or kid)
            accounts[identity] = {"client_id": client_id, "kid": kid, "key": key}
        except (KeyError, ValueError, TypeError):
            continue
    if len(accounts) != 1:
        raise ExportError("Need exactly one game/account with a successful login-by-token response; narrow the HAR or use --client-id")
    return next(iter(accounts.values()))


def path_and_query(url):
    parts = urllib.parse.urlsplit(url)
    return (parts.path or "/") + ("?" + parts.query if parts.query else "")


def app_message(method, url, header_items, body=b""):
    grouped = {}
    for name, value in header_items:
        name = name.lower()
        if name.startswith("x-tap-") and name != "x-tap-sign":
            grouped.setdefault(name, []).append(value)
    headers = "\n".join(name + ":" + ",".join(grouped[name]) for name in sorted(grouped))
    return (method + "\n" + path_and_query(url) + "\n" + headers + "\n").encode() + body + b"\n"


def app_signature(key, message):
    return base64.b64encode(hmac.new(key.encode(), message, hashlib.sha256).digest()).decode()


def mac_authorization(account, url, method="GET", timestamp=None, nonce=None):
    parts = urllib.parse.urlsplit(url)
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    nonce = secrets.token_hex(8) if nonce is None else nonce
    normalized = f"{timestamp}\n{nonce}\n{method}\n{path_and_query(url)}\n{parts.hostname}\n{parts.port or 443}\n\n"
    mac = base64.b64encode(hmac.new(account["key"].encode(), normalized.encode(), hashlib.sha1).digest()).decode()
    return f'MAC id="{account["kid"]}",ts="{timestamp}",nonce="{nonce}",mac="{mac}"'


def metadata_candidates(data):
    if len(data) < 24 or len(data) > MAX_METADATA:
        raise ExportError("Invalid metadata size")
    magic, version, offset, length, data_offset, data_length = struct.unpack_from("<6I", data)
    if magic != 0xFAB11BAF or version != 27:
        raise ExportError("Only the verified IL2CPP metadata v27 layout is supported")
    if length % 8 or offset < 24 or data_offset < 24 or offset + length > len(data) or data_offset + data_length > len(data):
        raise ExportError("Invalid metadata table bounds")
    found = set()
    for pos in range(offset, offset + length, 8):
        size, index = struct.unpack_from("<II", data, pos)
        if index + size > data_length:
            raise ExportError("Invalid metadata string bounds")
        if 16 <= size <= 128:
            value = data[data_offset + index:data_offset + index + size]
            if re.fullmatch(rb"[a-zA-Z0-9_-]+", value):
                found.add(value.decode("ascii"))
    return found


def recover_token(entries, metadata, client_id):
    captures = []
    for entry in entries:
        request = entry.get("request", {})
        url = request.get("url", "")
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.netloc != API_HOST or parts.path != "/sdk-core/v1/gatekeeper" or client_id_from_url(url) != client_id:
            continue
        items = [(h["name"], h["value"]) for h in request.get("headers", [])]
        signs = [value for name, value in items if name.lower() == "x-tap-sign"]
        if len(signs) != 1:
            continue
        if "text" not in request.get("postData", {}):
            continue
        try:
            expected = base64.b64decode(signs[0], validate=True)
        except (ValueError, binascii.Error):
            continue
        if len(expected) == 32:
            message = app_message(request["method"], url, items, request["postData"]["text"].encode())
            captures.append((message, expected))
    if not captures:
        raise ExportError("Need a gatekeeper request including X-Tap-Sign and its exact request body")
    matches = [key for key in metadata_candidates(metadata) if all(hmac.compare_digest(hmac.new(key.encode(), msg, hashlib.sha256).digest(), expected) for msg, expected in captures)]
    if len(matches) != 1:
        raise ExportError("No unique matching client token in this metadata; check game, version, and HAR completeness")
    return matches[0]


class RemoteZIP(io.RawIOBase):
    """Bounded HTTP Range reader; never falls back to downloading the entire APK."""

    def __init__(self, url):
        if urllib.parse.urlsplit(url).scheme != "https":
            raise ExportError("APK URL must use HTTPS")
        self.url, self.pos, self.transferred = url, 0, 0
        request = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            match = re.fullmatch(r"bytes 0-0/(\d+)", response.headers.get("Content-Range", ""))
            if response.status != 206 or not match or urllib.parse.urlsplit(response.url).scheme != "https":
                raise ExportError("Server does not support HTTPS byte ranges")
            self.size = int(match[1])
            self.url = response.url
            response.read(1)

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        if self.pos < 0:
            raise ExportError("Invalid ZIP seek")
        return self.pos

    def read(self, size=-1):
        size = min(self.size - self.pos, self.size if size < 0 else size)
        if size <= 0:
            return b""
        if size > MAX_METADATA or self.transferred + size > 150_000_000:
            raise ExportError("APK range transfer limit exceeded")
        start, end = self.pos, self.pos + size - 1
        request = urllib.request.Request(self.url, headers={"Range": f"bytes={start}-{end}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end}/{self.size}" or urllib.parse.urlsplit(response.url).scheme != "https":
                raise ExportError("Server returned the wrong byte range")
            data = response.read(size + 1)
        if len(data) != size:
            raise ExportError("Incomplete APK byte range")
        self.pos += size
        self.transferred += size
        return data


def extract_metadata(args):
    source = args.apk if args.apk else RemoteZIP(args.url_file.read_text().strip())
    with zipfile.ZipFile(source) as archive:
        info = archive.getinfo(METADATA_PATH)
        if info.file_size > MAX_METADATA or info.compress_size > MAX_METADATA:
            raise ExportError("Metadata exceeds size limit")
        data = archive.read(info)
    metadata_candidates(data)  # Check the supported layout before saving.
    private_write(args.output, data)
    print("Metadata extracted. Treat it as private: it may contain application credentials.")


def list_saves(account, client_token):
    url = f"https://{API_HOST}/cloud-save/v1/get-saves?" + urllib.parse.urlencode({"client_id": account["client_id"]})
    headers = {
        "User-Agent": "TapSDK-iOS/4.10.2", "Accept": "application/json",
        "X-Tap-PN": "TapSDK", "X-Tap-Platform": "iOS", "X-Tap-SDK-Artifact": "iOS",
        "X-Tap-SDK-Module": "TapCloudSave", "X-Tap-SDK-Module-Version": "4.10.2",
        "X-Tap-Lang": "zh_CN", "X-Tap-Ts": str(int(time.time())),
        "X-Tap-Nonce": secrets.token_hex(5), "X-Tap-Device-Id": str(uuid.uuid4()),
        "Authorization": mac_authorization(account, url),
    }
    headers["X-Tap-Sign"] = app_signature(client_token, app_message("GET", url, headers.items()))
    # Do not forward account credentials through a redirect to a different host.
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(urllib.request.Request(url, headers=headers, method="GET"), timeout=30) as response:
        data = json.loads(response.read(2_000_001))
    if data.get("success") is not True or not isinstance(data.get("data", {}).get("saves"), list):
        raise ExportError("Cloud save query failed or returned an unsupported response")
    return data["data"]["saves"]


def save_url(save):
    host, path = save["host"], save["save_path"]
    if "://" not in host:
        host = "https://" + host
    parsed = urllib.parse.urlsplit(host)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ExportError("Invalid cloud storage host")
    url = urllib.parse.urljoin(host.rstrip("/") + "/", path)
    target = urllib.parse.urlsplit(url)
    if target.scheme != "https" or target.netloc != parsed.netloc:
        raise ExportError("Cloud save path changes the storage origin")
    return url


def download_save(save, output, index):
    expected = save["save_size"]
    if not isinstance(expected, int) or not 0 < expected <= MAX_SAVE:
        raise ExportError("Invalid or oversized cloud save")
    # The CDN receives no account token, Cookie, or SDK authentication headers.
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(save_url(save), timeout=30) as response:
        raw = response.read(expected + 1)
    if len(raw) != expected:
        raise ExportError("Downloaded size differs from cloud metadata")
    stem = f"save-{index:03d}"
    archive = zipfile.is_zipfile(io.BytesIO(raw))
    if archive:
        with zipfile.ZipFile(io.BytesIO(raw)) as zipped:
            infos = zipped.infolist()
            if len(infos) > 1000 or sum(item.file_size for item in infos) > 200_000_000:
                raise ExportError("ZIP exceeds validation limits")
            if zipped.testzip() is not None:
                raise ExportError("ZIP CRC check failed")
    filename = stem + (".zip" if archive else ".bin")
    private_write(output / filename, raw)
    # Never retain signed URLs, identities, archive descriptions, or account credentials.
    report = {
        "file": filename, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
        "zip_crc_verified": archive, "cloud_modified_time": save.get("modified_time"),
        "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    private_write(output / (stem + ".json"), json.dumps(report, indent=2).encode())
    print(f"Saved {filename}: {len(raw)} bytes; SHA-256 {report['sha256']}")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    metadata = commands.add_parser("metadata", help="Extract IL2CPP metadata without running an APK")
    source = metadata.add_mutually_exclusive_group(required=True)
    source.add_argument("--apk", type=Path)
    source.add_argument("--url-file", type=Path, help="Private text file containing an HTTPS APK download URL")
    metadata.add_argument("--output", required=True, type=Path)
    for name in ["recover-token", "export"]:
        command = commands.add_parser(name)
        command.add_argument("--har", required=True, type=Path)
        command.add_argument("--client-id", help="Select one game when the HAR includes multiple games")
        if name == "recover-token":
            command.add_argument("--metadata", required=True, type=Path)
        else:
            command.add_argument("--client-token-file", required=True, type=Path)
        command.add_argument("--output", required=True, type=Path)
    return result


def main():
    args = parser().parse_args()
    try:
        if args.command == "metadata":
            extract_metadata(args)
        else:
            entries = load_har(args.har)
            account = select_account(entries, args.client_id)
            if args.command == "recover-token":
                token = recover_token(entries, args.metadata.read_bytes(), account["client_id"])
                private_write(args.output, token.encode())
                print("Client token matched captured signatures and was saved privately; value not printed.")
            else:
                token = args.client_token_file.read_text().strip()
                if not token:
                    raise ExportError("Client token file is empty")
                saves = list_saves(account, token)
                if not saves:
                    raise ExportError("No cloud saves found; check the account and manual cloud upload")
                args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
                for index, save in enumerate(saves, 1):
                    download_save(save, args.output, index)
    except urllib.error.HTTPError as error:
        print(f"HTTP {error.code}: request failed. Check token validity, client token, and SDK compatibility. Response and URL omitted.", file=sys.stderr)
        return 1
    except (ExportError, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, urllib.error.URLError) as error:
        # Raw exceptions can include URLs, local identities, or server response content.
        message = str(error) if isinstance(error, ExportError) else "Input, file, archive, or network error; details omitted to avoid exposing private data"
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
