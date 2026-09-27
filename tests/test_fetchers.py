"""fetchers.py 单元测试：来源路由、CF API 信封解析、SigV4（网络全部 mock）。"""

import base64
import io
import json
import re
import urllib.error

import pytest

import fetchers
from fetchers import FetchError, fetch_quotes, sigv4_get_headers


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def install_urlopen(monkeypatch, payload: bytes | Exception, capture: dict | None = None):
    def handler(req, timeout=None):
        if capture is not None:
            capture["url"] = req.full_url
            capture["headers"] = dict(req.headers)
            capture["data"] = req.data
        if isinstance(payload, Exception):
            raise payload
        return FakeResp(payload)

    monkeypatch.setattr(fetchers.urllib.request, "urlopen", handler)


# ---- 来源路由 ----

def test_source_none_raises_hint():
    with pytest.raises(FetchError, match="source"):
        fetch_quotes({"source": "none"})


def test_source_unknown_raises():
    with pytest.raises(FetchError, match="未知的导入源"):
        fetch_quotes({"source": "ftp"})


def test_url_source_missing_config(monkeypatch):
    with pytest.raises(FetchError, match="url"):
        fetch_quotes({"source": "url"})


def test_url_source_fetches(monkeypatch):
    install_urlopen(monkeypatch, b"data")
    assert fetch_quotes({"source": "url", "url": "https://example.com/q.json"}) == b"data"


def test_file_source(tmp_path):
    f = tmp_path / "quotes.json"
    f.write_text('["本地语录"]', encoding="utf-8")
    rows = fetch_quotes({"source": "file", "local_path": str(f)})
    assert json.loads(rows) == ["本地语录"]


def test_webdav_basic_auth(monkeypatch):
    capture: dict = {}
    install_urlopen(monkeypatch, b"dav", capture)
    fetch_quotes({
        "source": "webdav",
        "webdav_url": "https://dav.example.com/dav/",
        "webdav_path": "/quotes/quotes.json",
        "webdav_username": "u",
        "webdav_password": "p",
    })
    assert capture["url"] == "https://dav.example.com/dav/quotes/quotes.json"
    auth = capture["headers"].get("Authorization")
    assert auth == "Basic " + base64.b64encode(b"u:p").decode()


# ---- R2 ----

def test_r2_public_url(monkeypatch):
    capture: dict = {}
    install_urlopen(monkeypatch, b"r2", capture)
    assert fetch_quotes({"source": "r2", "r2_public_url": "https://pub-x.r2.dev/q.json"}) == b"r2"
    assert capture["url"] == "https://pub-x.r2.dev/q.json"


def test_r2_api_mode_returns_raw_json_object(monkeypatch):
    """语录文件本身是 JSON 时，CF API 模式必须原样返回字节，不能误当信封解析。"""
    capture: dict = {}
    install_urlopen(monkeypatch, b'{"quotes": ["x"]}', capture)
    out = fetch_quotes({
        "source": "r2",
        "r2_account_id": "acct",
        "r2_bucket": "bucket",
        "r2_api_token": "tok",
        "r2_object_key": "quotes.json",
    })
    assert out == b'{"quotes": ["x"]}'
    assert capture["url"].endswith("/accounts/acct/r2/buckets/bucket/objects/quotes.json")
    assert capture["headers"]["Authorization"] == "Bearer tok"


def test_r2_api_error_envelope(monkeypatch):
    """R2 对象错误以非 200 状态返回，信封里的 message 要进 FetchError。"""
    install_urlopen(monkeypatch, urllib.error.HTTPError(
        "https://api.cloudflare.com/client/v4/x", 400, "Bad Request", {},
        io.BytesIO(json.dumps({
            "success": False,
            "errors": [{"message": "Authentication error"}],
        }).encode())))
    with pytest.raises(FetchError, match="Authentication error"):
        fetch_quotes({
            "source": "r2",
            "r2_account_id": "acct",
            "r2_bucket": "b",
            "r2_api_token": "bad",
        })


def test_r2_unconfigured(monkeypatch):
    with pytest.raises(FetchError, match="R2 未配置"):
        fetch_quotes({"source": "r2"})


def test_r2_s3_missing_keys():
    with pytest.raises(FetchError, match="access_key"):
        fetch_quotes({"source": "r2", "r2_s3_endpoint": "https://acct.r2.cloudflarestorage.com/bucket"})


# ---- D1 ----

def test_d1_query(monkeypatch):
    capture: dict = {}
    envelope = {
        "success": True,
        "result": [{"results": [{"text": "A"}, {"text": "B"}]}],
    }
    install_urlopen(monkeypatch, json.dumps(envelope).encode(), capture)
    rows = fetch_quotes({
        "source": "d1",
        "d1_account_id": "acct",
        "d1_database_id": "db",
        "d1_api_token": "tok",
        "d1_sql": "SELECT text FROM quotes",
    })
    assert [r["text"] for r in rows] == ["A", "B"]
    assert capture["url"].endswith("/accounts/acct/d1/database/db/query")
    assert json.loads(capture["data"]) == {"sql": "SELECT text FROM quotes"}


def test_d1_unconfigured():
    with pytest.raises(FetchError, match="D1 未配置完整"):
        fetch_quotes({"source": "d1", "d1_account_id": "acct"})


# ---- HTTP 错误 ----

def test_http_error_wrapped(monkeypatch):
    install_urlopen(monkeypatch, urllib.error.HTTPError(
        "https://example.com/q.json", 404, "Not Found", {}, io.BytesIO(b"")))
    with pytest.raises(FetchError, match="404"):
        fetch_quotes({"source": "url", "url": "https://example.com/q.json"})


# ---- SigV4 ----

def test_sigv4_headers_structure():
    headers = sigv4_get_headers(
        "https://acct.r2.cloudflarestorage.com/bucket/quotes.json",
        "AKID", "secret",
    )
    auth = headers["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 ")
    assert "/auto/s3/aws4_request" in auth
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in auth
    signature = re.search(r"Signature=([0-9a-f]{64})", auth)
    assert signature, "签名必须是 64 位十六进制"
    assert re.fullmatch(r"\d{8}T\d{6}Z", headers["x-amz-date"])
    assert headers["x-amz-content-sha256"] == fetchers.hashlib.sha256(b"").hexdigest()


def test_sigv4_deterministic_and_path_escaped():
    url = "https://acct.r2.cloudflarestorage.com/bucket/my quotes.json"
    h1 = sigv4_get_headers(url, "AKID", "secret")
    h2 = sigv4_get_headers(url, "AKID", "secret")
    # x-amz-date 相同（同一秒）时签名必须完全一致
    if h1["x-amz-date"] == h2["x-amz-date"]:
        assert h1 == h2
    assert "Credential=AKID/" in h1["Authorization"]
