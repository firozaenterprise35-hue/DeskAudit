"""Tests for the Google Drive auto-upload (v12.16).

Network is fully mocked: urllib.request.urlopen is replaced with a
fake that answers the token endpoint, the folder lookup/creation and
the multipart upload, so no real Google traffic happens.
"""

import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import deskaudit.drive_upload as du  # noqa: E402


class _FakeResp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run_with_fake(handler, fn):
    old = urllib.request.urlopen
    urllib.request.urlopen = handler
    try:
        return fn()
    finally:
        urllib.request.urlopen = old


def test_auth_url():
    url = du.build_auth_url("cid123.apps.googleusercontent.com")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=cid123.apps.googleusercontent.com" in url
    assert "scope=" in url and "drive.file" in url
    assert "access_type=offline" in url
    assert "response_type=code" in url
    print("auth url passed")


def test_exchange_code():
    seen = {}

    def fake(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = req.data.decode("utf-8")
        return _FakeResp({"access_token": "acc1", "refresh_token": "ref1",
                          "expires_in": 3600, "token_type": "Bearer"})

    result = _run_with_fake(fake, lambda: du.exchange_code("cid", "csec", "  code99 "))
    assert result["refresh_token"] == "ref1"
    assert seen["url"] == du.TOKEN_URL
    assert "grant_type=authorization_code" in seen["body"]
    assert "code=code99" in seen["body"]  # stripped
    print("exchange code passed")


def test_exchange_code_missing_refresh():
    def fake(req, timeout=None):
        return _FakeResp({"access_token": "acc1"})

    try:
        _run_with_fake(fake, lambda: du.exchange_code("cid", "csec", "code"))
    except du.DriveError as exc:
        assert "টোকেন" in str(exc)
    else:
        raise AssertionError("expected DriveError")
    print("exchange code missing refresh passed")


def test_upload_creates_folder_and_uploads():
    calls = []

    def fake(req, timeout=None):
        calls.append(req.full_url)
        if req.full_url.startswith(du.FILES_URL) and req.get_method() == "GET":
            return _FakeResp({"files": []})  # folder missing
        if req.full_url == du.FILES_URL and req.get_method() == "POST":
            return _FakeResp({"id": "fld1", "name": "DeskAudit Recordings"})
        if req.full_url == du.UPLOAD_URL:
            body = req.data
            assert b"DeskAudit Recordings" not in body  # folder id, not name
            assert b"fld1" in body
            assert b"hello-drive" in body
            return _FakeResp({"id": "file1",
                              "webViewLink": "https://drive.google.com/file/d/file1/view"})
        raise AssertionError(f"unexpected call {req.full_url}")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rec.mp4")
        with open(path, "wb") as f:
            f.write(b"hello-drive")
        result = _run_with_fake(fake, lambda: du.upload_file("acc1", path))
    assert result["id"] == "file1"
    assert "file1" in result["webViewLink"]
    assert len(calls) == 3  # find folder, create folder, upload
    print("upload creates folder passed")


def test_upload_reuses_existing_folder():
    calls = []

    def fake(req, timeout=None):
        calls.append((req.full_url, req.get_method()))
        if req.get_method() == "GET":
            return _FakeResp({"files": [{"id": "fld9", "name": "DeskAudit Recordings"}]})
        if req.full_url == du.UPLOAD_URL:
            return _FakeResp({"id": "file2", "webViewLink": "https://x/file2"})
        raise AssertionError(f"unexpected call {req.full_url}")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rec.mp4")
        with open(path, "wb") as f:
            f.write(b"data")
        result = _run_with_fake(fake, lambda: du.upload_file("acc1", path))
    assert result["id"] == "file2"
    assert len(calls) == 2  # find folder, upload (no create)
    print("upload reuses folder passed")


def test_uploader_refreshes_and_retries_401():
    calls = []

    def fake(req, timeout=None):
        calls.append(req.full_url)
        if req.full_url == du.TOKEN_URL:
            return _FakeResp({"access_token": "acc2", "expires_in": 3600})
        if req.full_url.startswith(du.FILES_URL) and req.get_method() == "GET":
            return _FakeResp({"files": [{"id": "fld1"}]})
        if req.full_url == du.UPLOAD_URL:
            if len([c for c in calls if c == du.UPLOAD_URL]) == 1:
                raise urllib.error.HTTPError(
                    req.full_url, 401, "Unauthorized", {},
                    io.BytesIO(b'{"error": {"message": "Invalid Credentials"}}'))
            return _FakeResp({"id": "file3", "webViewLink": "https://x/file3"})
        raise AssertionError(f"unexpected call {req.full_url}")

    up = du.DriveUploader({"client_id": "cid", "client_secret": "csec",
                           "refresh_token": "ref1", "enabled": True})
    assert up.configured is True
    assert up.enabled is True
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rec.mp4")
        with open(path, "wb") as f:
            f.write(b"data")
        ok, link = _run_with_fake(fake, lambda: up.upload(path))
    assert ok is True and "file3" in link
    assert [c for c in calls if c == du.UPLOAD_URL].__len__() == 2  # retried once
    print("uploader 401 retry passed")


def test_uploader_not_enabled():
    up = du.DriveUploader({"client_id": "", "client_secret": "",
                           "refresh_token": ""})
    assert up.configured is False
    assert up.enabled is False
    assert up.auth_url() is None
    ok, msg = up.upload("nope.mp4")
    assert ok is False
    print("uploader not enabled passed")


def test_uploader_upload_failure_returns_error():
    def fake(req, timeout=None):
        if req.full_url == du.TOKEN_URL:
            return _FakeResp({"access_token": "acc1", "expires_in": 3600})
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", {},
            io.BytesIO(b'{"error": {"message": "The user has not granted the app drive.file scope."}}'))

    up = du.DriveUploader({"client_id": "cid", "client_secret": "csec",
                           "refresh_token": "ref1", "enabled": True})
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rec.mp4")
        with open(path, "wb") as f:
            f.write(b"data")
        ok, msg = _run_with_fake(fake, lambda: up.upload(path))
    assert ok is False
    assert "drive.file" in msg
    print("uploader upload failure passed")


if __name__ == "__main__":
    test_auth_url()
    test_exchange_code()
    test_exchange_code_missing_refresh()
    test_upload_creates_folder_and_uploads()
    test_upload_reuses_existing_folder()
    test_uploader_refreshes_and_retries_401()
    test_uploader_not_enabled()
    test_uploader_upload_failure_returns_error()
    print("ALL DRIVE UPLOAD TESTS PASSED")
